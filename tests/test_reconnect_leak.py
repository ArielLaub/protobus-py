"""
Regression tests for the reconnection resource leak.

Reconnect churn used to grow AMQP resources without bound: every
``reconnected`` event made each listener and dispatcher open a *fresh*
channel while dropping the previous one without closing it, and
``MessageDispatcher`` additionally built a whole new ``CallbackListener``
(channel + exclusive queue + consumer) each time and never closed the old
one. Because every leaked ``CallbackListener`` stayed registered on the
connection's ``reconnected``/``disconnected`` events, the growth compounded:
N reconnects produced O(N^2) channels.

Measured on a shared broker on 2026-08-18 (two Python services):
199 connections / 30,037 channels and 194 / 7,189, climbing ~260
channels/sec, with consumers going 13,442 -> 15,358 in 15s. Restarting the
two pods took the broker from 26,429 channels / 398 connections / 602MB to
48 / 9 / 144MB.

These tests assert *stability*, not merely that reconnection works: channel
and consumer counts must stay at their steady-state value across N simulated
reconnects. No broker is needed — ``FakeConnection`` counts ``open_channel``
and ``close`` calls, which is the level the bug lives at.
"""

import asyncio
import inspect
from typing import Any, Callable, Dict, List, Optional

import pytest

from protobus.callback_listener import CallbackListener
from protobus.event_dispatcher import EventDispatcher
from protobus.message_dispatcher import MessageDispatcher
from protobus.message_factory import MessageFactory
from protobus.message_listener import MessageListener

RECONNECT_CYCLES = 10


class FakeChannel:
    """A channel that remembers whether anybody closed it."""

    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        self.closed = False
        self.prefetch_count: Optional[int] = None

    async def set_qos(self, prefetch_count: Optional[int] = None, **_: Any) -> None:
        self.prefetch_count = prefetch_count

    async def close(self) -> None:
        self.closed = True

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FakeChannel {self.channel_id} closed={self.closed}>"


class FakeExchange:
    def __init__(self, name: str, channel: FakeChannel):
        self.name = name
        self.channel = channel


class FakeQueue:
    def __init__(self, name: str, channel: FakeChannel, connection: "FakeConnection"):
        self.name = name
        self.channel = channel
        self._connection = connection

    async def cancel(self, consumer_tag: str) -> None:
        self._connection.consumers.pop(consumer_tag, None)


class FakeConnection:
    """
    Minimal stand-in for ``protobus.connection.Connection``.

    Implements the surface ``BaseListener`` / the dispatchers actually use, and
    counts AMQP objects so a leak is directly observable.
    """

    def __init__(self) -> None:
        self.is_connected = True
        self.channels: List[FakeChannel] = []
        self.consumers: Dict[str, str] = {}
        self.bindings: List[str] = []
        self._event_handlers: Dict[str, List[Callable[..., Any]]] = {}
        self._next_channel = 0
        self._next_consumer = 0
        self._next_queue = 0

    # --- observability used by the assertions -------------------------------

    @property
    def open_channels(self) -> List[FakeChannel]:
        """Channels opened and not closed — the number RabbitMQ would report."""
        return [c for c in self.channels if not c.closed]

    def handler_count(self, event: str) -> int:
        return len(self._event_handlers.get(event, []))

    # --- Connection surface -------------------------------------------------

    async def open_channel(self) -> FakeChannel:
        channel = FakeChannel(self._next_channel)
        self._next_channel += 1
        self.channels.append(channel)
        return channel

    async def ensure_exchange(
        self, channel: FakeChannel, name: str, exchange_type: Any = None
    ) -> FakeExchange:
        return FakeExchange(name, channel)

    async def ensure_queue(
        self,
        channel: FakeChannel,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> FakeQueue:
        if not name:
            # Mirrors the broker: an anonymous queue gets a fresh generated name.
            name = f"amq.gen-{self._next_queue}"
            self._next_queue += 1
        elif name.startswith("amq."):
            # Mirrors the broker: amq.* is reserved, declaring one by name is
            # ACCESS_REFUSED. Re-declaring a previous anonymous queue's
            # generated name after a reconnect is therefore always a bug.
            raise RuntimeError(f"ACCESS_REFUSED - queue name '{name}' is reserved")
        return FakeQueue(name, channel, self)

    async def bind_queue(
        self, queue: FakeQueue, exchange: FakeExchange, routing_key: str
    ) -> None:
        self.bindings.append(f"{queue.name}:{exchange.name}:{routing_key}")

    async def consume(
        self,
        channel: FakeChannel,
        queue: FakeQueue,
        handler: Callable[..., Any],
        late_ack: bool = False,
        max_concurrent: Optional[int] = None,
        retry_options: Any = None,
    ) -> str:
        tag = f"ctag-{self._next_consumer}"
        self._next_consumer += 1
        self.consumers[tag] = queue.name
        return tag

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        self._event_handlers.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Callable[..., Any]) -> None:
        handlers = self._event_handlers.get(event)
        if not handlers:
            return
        try:
            handlers.remove(callback)
        except ValueError:
            return
        if not handlers:
            del self._event_handlers[event]

    # --- test driver --------------------------------------------------------

    async def emit(self, event: str, *args: Any) -> None:
        """
        Fire an event the way ``Connection._emit`` does, but deterministically:
        coroutine handlers are awaited instead of being turned into tasks, and
        the loop is drained afterwards so fire-and-forget cleanup can run.
        """
        for handler in list(self._event_handlers.get(event, [])):
            result = handler(*args)
            if inspect.isawaitable(result):
                await result
        for _ in range(5):
            await asyncio.sleep(0)

    async def reconnect_cycle(self, cycles: int = RECONNECT_CYCLES) -> None:
        """Simulate ``cycles`` broker flaps: disconnected then reconnected."""
        for _ in range(cycles):
            await self.emit("disconnected")
            await self.emit("reconnected")


async def _noop_handler(
    data: bytes, correlation_id: str, headers: Optional[Dict[str, Any]] = None
) -> None:
    return None


@pytest.fixture
def connection() -> FakeConnection:
    return FakeConnection()


class TestListenerReconnectLeak:
    async def test_channel_and_consumer_counts_are_stable(self, connection):
        """A consuming listener must hold exactly one channel and one consumer forever."""
        listener = CallbackListener(connection)
        await listener.init(_noop_handler, "")
        await listener.start()

        assert len(connection.open_channels) == 1
        assert len(connection.consumers) == 1

        await connection.reconnect_cycle()

        assert len(connection.open_channels) == 1, (
            f"leaked channels: {len(connection.open_channels)} open after "
            f"{RECONNECT_CYCLES} reconnects"
        )
        assert len(connection.consumers) == 1, (
            f"leaked consumers: {len(connection.consumers)} live after "
            f"{RECONNECT_CYCLES} reconnects"
        )

    async def test_bare_reconnected_event_does_not_strand_a_live_channel(
        self, connection
    ):
        """
        `reconnected` fires unconditionally, without a preceding `disconnected`
        too. Here the previous channel is still *live*, so re-setup has to close
        it rather than just overwrite the reference.
        """
        listener = CallbackListener(connection)
        await listener.init(_noop_handler, "")
        await listener.start()

        for _ in range(RECONNECT_CYCLES):
            await connection.emit("reconnected")

        assert len(connection.open_channels) == 1, (
            f"stranded channels: {len(connection.open_channels)} open after "
            f"{RECONNECT_CYCLES} bare reconnected events"
        )
        assert len(connection.consumers) == 1

    async def test_reconnect_before_init_opens_nothing(self, connection):
        """An un-initialized listener must not react to reconnection at all."""
        CallbackListener(connection)

        await connection.reconnect_cycle(5)

        assert connection.channels == []
        assert connection.consumers == {}

    async def test_close_unregisters_connection_handlers(self, connection):
        listener = CallbackListener(connection)
        await listener.init(_noop_handler, "")
        await listener.start()

        await listener.close()

        assert connection.handler_count("reconnected") == 0
        assert connection.handler_count("disconnected") == 0
        assert connection.open_channels == []
        assert connection.consumers == {}

        # A late event on a closed listener must be inert.
        await connection.reconnect_cycle(3)
        assert connection.open_channels == []


class TestNamedListenerReconnectLeak:
    async def test_named_queue_listener_is_stable_and_rebinds(self, connection):
        """A named-queue topic listener keeps one channel, one consumer, and its bindings."""
        listener = MessageListener(connection)
        await listener.init(_noop_handler, "test.queue")
        await listener.subscribe("REQUEST.test.queue.*")
        await listener.start()

        assert len(connection.open_channels) == 1
        assert len(connection.consumers) == 1
        bindings_after_init = len(connection.bindings)

        await connection.reconnect_cycle()

        assert len(connection.open_channels) == 1, (
            f"leaked channels: {len(connection.open_channels)} open after "
            f"{RECONNECT_CYCLES} reconnects"
        )
        assert len(connection.consumers) == 1
        assert listener.queue_name == "test.queue"
        # The subscription is re-established on every reconnect, not lost.
        assert len(connection.bindings) == bindings_after_init + RECONNECT_CYCLES


class TestMessageDispatcherReconnectLeak:
    async def test_channel_and_consumer_counts_are_stable(self, connection):
        """
        A dispatcher holds two channels — its own and its CallbackListener's —
        and one consumer, no matter how often the connection flaps.
        """
        dispatcher = MessageDispatcher(connection)
        await dispatcher.init()

        assert len(connection.open_channels) == 2
        assert len(connection.consumers) == 1

        await connection.reconnect_cycle()

        assert len(connection.open_channels) == 2, (
            f"leaked channels: {len(connection.open_channels)} open after "
            f"{RECONNECT_CYCLES} reconnects"
        )
        assert len(connection.consumers) == 1, (
            f"leaked consumers: {len(connection.consumers)} live after "
            f"{RECONNECT_CYCLES} reconnects"
        )

    async def test_callback_listeners_do_not_accumulate(self, connection):
        """
        The dispatcher must reuse its CallbackListener. A new one per reconnect
        also registers new connection event handlers, which is what turned a
        linear leak into a quadratic one.
        """
        dispatcher = MessageDispatcher(connection)
        await dispatcher.init()
        baseline = connection.handler_count("reconnected")

        await connection.reconnect_cycle()

        assert connection.handler_count("reconnected") == baseline
        assert connection.handler_count("disconnected") == baseline

    async def test_bare_reconnected_event_does_not_strand_a_live_channel(
        self, connection
    ):
        dispatcher = MessageDispatcher(connection)
        await dispatcher.init()

        for _ in range(RECONNECT_CYCLES):
            await connection.emit("reconnected")

        assert len(connection.open_channels) == 2, (
            f"stranded channels: {len(connection.open_channels)} open after "
            f"{RECONNECT_CYCLES} bare reconnected events"
        )
        assert len(connection.consumers) == 1

    async def test_reconnect_before_init_opens_nothing(self, connection):
        MessageDispatcher(connection)

        await connection.reconnect_cycle(5)

        assert connection.channels == []

    async def test_close_releases_everything(self, connection):
        dispatcher = MessageDispatcher(connection)
        await dispatcher.init()

        await dispatcher.close()

        assert connection.handler_count("reconnected") == 0
        assert connection.handler_count("disconnected") == 0
        assert connection.open_channels == []
        assert connection.consumers == {}


class TestEventDispatcherReconnectLeak:
    async def test_channel_count_is_stable(self, connection):
        dispatcher = EventDispatcher(connection, MessageFactory())
        await dispatcher.init()

        assert len(connection.open_channels) == 1

        await connection.reconnect_cycle()

        assert len(connection.open_channels) == 1, (
            f"leaked channels: {len(connection.open_channels)} open after "
            f"{RECONNECT_CYCLES} reconnects"
        )

    async def test_close_unregisters_connection_handlers(self, connection):
        dispatcher = EventDispatcher(connection, MessageFactory())
        await dispatcher.init()

        await dispatcher.close()

        assert connection.handler_count("reconnected") == 0
        assert connection.handler_count("disconnected") == 0
        assert connection.open_channels == []


class FakeCallbackCollection:
    """Stands in for aio_pika's CallbackCollection."""

    def __init__(self) -> None:
        self._callbacks: List[Callable[..., Any]] = []

    def add(self, callback: Callable[..., Any]) -> None:
        self._callbacks.append(callback)

    def remove(self, callback: Callable[..., Any]) -> None:
        self._callbacks.remove(callback)

    def __contains__(self, callback: Callable[..., Any]) -> bool:
        return callback in self._callbacks

    def __len__(self) -> int:
        return len(self._callbacks)


class FakeRobustConnection:
    """Stands in for the object aio_pika.connect_robust returns."""

    def __init__(self, url: str):
        self.url = url
        self.closed = False
        self.close_callbacks = FakeCallbackCollection()

    async def close(self) -> None:
        self.closed = True


class TestConnectionReconnectLeak:
    """
    The connection object itself leaked on every reconnect.

    ``Connection._reconnect`` replaced ``self._connection`` with a brand-new
    ``connect_robust`` result without closing the previous one and without
    detaching ``_on_connection_closed`` from it. Since a RobustConnection
    re-establishes itself, each abandoned connection came back with its
    channels and consumers still attached at the broker — and, still holding
    our close callback, spawned yet another reconnect on its next flap. This is
    the half that explains 199 *connections* for one pod.
    """

    @pytest.fixture
    def created(self, monkeypatch) -> List[FakeRobustConnection]:
        import protobus.connection as connection_module

        made: List[FakeRobustConnection] = []

        async def fake_connect_robust(url: str, **kwargs: Any) -> FakeRobustConnection:
            conn = FakeRobustConnection(url)
            made.append(conn)
            return conn

        monkeypatch.setattr(
            connection_module.aio_pika, "connect_robust", fake_connect_robust
        )
        return made

    async def _flap(self, conn: Any, dead: FakeRobustConnection) -> None:
        conn._on_connection_closed(dead, None)
        if conn._reconnect_task:
            await conn._reconnect_task
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_replaced_connection_is_closed_and_detached(self, created):
        from protobus.connection import Connection

        conn = Connection()
        await conn.connect("amqp://guest:guest@localhost:5672/")
        first = created[0]

        await self._flap(conn, first)

        assert len(created) == 2
        assert first.closed, "the replaced connection was never closed"
        assert conn._on_connection_closed not in first.close_callbacks, (
            "the replaced connection still holds our close callback and can "
            "spawn further reconnects"
        )
        assert conn.is_connected

    async def test_live_connection_count_is_stable_across_flaps(self, created):
        from protobus.connection import Connection

        conn = Connection()
        await conn.connect("amqp://guest:guest@localhost:5672/")

        for _ in range(RECONNECT_CYCLES):
            await self._flap(conn, created[-1])

        live = [c for c in created if not c.closed]
        assert len(live) == 1, (
            f"leaked connections: {len(live)} live after {RECONNECT_CYCLES} flaps"
        )

    async def test_simultaneous_close_callbacks_spawn_one_reconnect(self, created):
        """Two close callbacks in the same tick must not race into two connections."""
        from protobus.connection import Connection

        conn = Connection()
        await conn.connect("amqp://guest:guest@localhost:5672/")
        first = created[0]

        conn._on_connection_closed(first, None)
        conn._on_connection_closed(first, None)
        if conn._reconnect_task:
            await conn._reconnect_task
        for _ in range(5):
            await asyncio.sleep(0)

        assert len(created) == 2, (
            f"{len(created) - 1} reconnects were started for one disconnect"
        )
