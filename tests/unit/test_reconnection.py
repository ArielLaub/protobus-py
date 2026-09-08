"""
Reconnection: single-flight connect, generation invalidation, coordinated
restoration, readiness, and the disconnect race.
"""

import asyncio
from typing import List

import pytest

import protobus.connection as connection_module
from protobus import (
    BaseListener,
    Connection,
    LogLevel,
    MessageDispatcher,
    NotReadyError,
    ReconnectionOptions,
    get_log_level,
    set_log_level,
    set_logger,
)
from protobus.logger import DefaultLogger

from ..helpers import FakeHandle, tick

FAST = ReconnectionOptions(max_retries=5, initial_delay_ms=1, max_delay_ms=5)


class CapturingLogger:
    def __init__(self):
        self.lines: List[str] = []

    def info(self, m):
        self.lines.append(str(m))

    warn = debug = error = info

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def handles(monkeypatch):
    """Patch aiormq.connect to hand out FakeHandles, recording each."""
    class Handles(list):
        behaviours: List = []

    created = Handles()
    behaviours = created.behaviours = []

    async def connect(url, *a, **kw):
        if behaviours:
            behaviour = behaviours.pop(0)
            result = behaviour() if callable(behaviour) else behaviour
            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, BaseException):
                raise result
            created.append(result)
            return result
        h = FakeHandle()
        created.append(h)
        return h

    monkeypatch.setattr(connection_module.aiormq, "connect", connect)
    return created


@pytest.fixture(autouse=True)
def _restore_logger():
    level = get_log_level()
    yield
    set_logger(DefaultLogger())
    set_log_level(level)


async def settle(seconds: float = 0.2):
    await asyncio.sleep(seconds)


class ProbeListener(BaseListener):
    def __init__(self, connection):
        super().__init__(connection)
        self._exchange_name = "probe.exchange"
        self._exchange_type = "topic"
        self.restores = 0
        self.fail_next_restore = False

    async def _reinitialize(self):
        self.restores += 1
        if self.fail_next_restore:
            self.fail_next_restore = False
            raise RuntimeError("queue declaration refused")
        await super()._reinitialize()


class TestReconnectionIsNotAnnouncedUntilTheTopologyIsBack:
    async def test_withholds_reconnected_until_every_restorer_has_resolved(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        order = []
        blocked = asyncio.Event()

        async def restore(_generation):
            order.append("restore:start")
            await blocked.wait()
            order.append("restore:done")

        conn.register_restorer(restore)
        conn.on("reconnected", lambda: order.append("reconnected"))

        handles[0].drop()
        await settle()
        assert order == ["restore:start"]
        assert conn.is_ready is False

        blocked.set()
        await settle()
        assert order == ["restore:start", "restore:done", "reconnected"]
        assert conn.is_ready is True
        await conn.disconnect()

    async def test_has_a_usable_dispatcher_channel_by_the_time_reconnected_fires(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        dispatcher = MessageDispatcher(conn)
        await dispatcher.init()
        seen = {}
        conn.on("reconnected", lambda: seen.__setitem__("channel", dispatcher.channel))

        handles[0].drop()
        await settle()
        assert seen.get("channel") is not None
        await dispatcher.close()
        await conn.disconnect()

    async def test_retries_instead_of_announcing_when_a_restorer_fails(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        attempts = {"n": 0}
        announced = {"n": 0}
        conn.on("reconnected", lambda: announced.__setitem__("n", announced["n"] + 1))
        conn.on("error", lambda _e: None)

        async def restore(_g):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("queue declaration refused")

        conn.register_restorer(restore)
        handles[0].drop()
        await settle(0.4)
        assert attempts["n"] >= 3
        assert announced["n"] == 1
        assert conn.is_ready is True
        # Every generation that could not be restored was closed.
        assert sum(1 for h in handles[1:] if not h.closed) == 1
        await conn.disconnect()

    async def test_gives_up_through_the_max_retries_budget_when_restoration_keeps_failing(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", ReconnectionOptions(max_retries=2, initial_delay_ms=1, max_delay_ms=5))
        errors = []
        conn.on("error", errors.append)

        async def restore(_g):
            raise RuntimeError("always broken")

        conn.register_restorer(restore)
        handles[0].drop()
        await settle(0.4)
        assert any("max reconnection attempts" in str(e) for e in errors)
        assert conn.is_ready is False
        await conn.disconnect()

    async def test_unregisters_a_restorer_so_a_closed_component_is_not_restored_again(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        calls = {"n": 0}

        async def restore(_g):
            calls["n"] += 1

        unregister = conn.register_restorer(restore)
        unregister()
        handles[0].drop()
        await settle()
        assert calls["n"] == 0
        await conn.disconnect()

    async def test_reports_a_listener_restore_failure_to_the_coordinator(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        listener = ProbeListener(conn)

        async def handler(*_a):
            return None

        await listener.init(handler, "probe.queue")
        listener.fail_next_restore = True
        announced = {"n": 0}
        conn.on("reconnected", lambda: announced.__setitem__("n", announced["n"] + 1))
        conn.on("error", lambda _e: None)

        handles[0].drop()
        await settle(0.4)
        assert listener.restores >= 2
        assert announced["n"] == 1
        await conn.disconnect()

    async def test_restorers_run_in_registration_order(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        order = []

        for name in ("a", "b", "c"):
            async def restore(_g, name=name):
                order.append(name)
                await asyncio.sleep(0.005)

            conn.register_restorer(restore)
        handles[0].drop()
        await settle()
        assert order == ["a", "b", "c"]
        await conn.disconnect()


class TestReadinessIsAvailableToPublishers:
    async def test_is_ready_after_the_initial_connect(self, handles):
        conn = Connection()
        assert conn.is_ready is False
        await conn.connect("amqp://localhost", FAST)
        assert conn.is_ready is True
        await conn.when_ready()
        await conn.disconnect()

    async def test_parks_when_ready_across_a_reconnection_and_resolves_once_restored(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        handles[0].drop()
        await tick()
        assert conn.is_ready is False
        waiting = asyncio.ensure_future(conn.when_ready())
        await settle()
        await waiting
        await conn.disconnect()

    async def test_rejects_a_parked_waiter_when_the_connection_is_deliberately_closed(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        handles[0].drop()
        await tick()
        waiting = asyncio.ensure_future(conn.when_ready())
        await tick()
        await conn.disconnect()
        with pytest.raises(NotReadyError):
            await waiting

    async def test_when_ready_times_out(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", ReconnectionOptions(max_retries=5, initial_delay_ms=5000))
        handles[0].drop()
        await tick()
        with pytest.raises(NotReadyError, match="within 20ms"):
            await conn.when_ready(20)
        await conn.disconnect()


class TestPublishersWaitThroughAReconnection:
    async def test_parks_a_publish_issued_mid_reconnection_and_completes_it_once_restored(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        dispatcher = MessageDispatcher(conn)
        await dispatcher.init()

        handles[0].drop()
        await tick()
        assert conn.is_connected is False
        in_flight = asyncio.ensure_future(dispatcher.publish(b"body", "REQUEST.P.S.go", False))
        await settle()
        assert await in_flight is None
        await dispatcher.close()
        await conn.disconnect()

    async def test_publishes_successfully_from_a_reconnected_handler(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        dispatcher = MessageDispatcher(conn)
        await dispatcher.init()
        outcome = {"value": "never ran"}

        async def on_reconnected():
            try:
                await dispatcher.publish(b"body", "REQUEST.P.S.go", False)
                outcome["value"] = "published"
            except Exception as err:
                outcome["value"] = f"failed: {err}"

        conn.on("reconnected", on_reconnected)
        handles[0].drop()
        await settle()
        assert outcome["value"] == "published"
        await dispatcher.close()
        await conn.disconnect()

    async def test_still_reports_a_never_connected_dispatcher_at_once(self):
        conn = Connection()
        dispatcher = MessageDispatcher(conn)
        with pytest.raises(Exception):
            await dispatcher.publish(b"b", "REQUEST.P.S.go", False)


class TestAnIConnectionWithoutRegisterRestorerStillReconnects:
    async def test_falls_back_to_the_reconnected_event(self):
        from ..helpers import FakeConnection

        legacy = FakeConnection(coordinated=False)
        dispatcher = MessageDispatcher(legacy)
        dispatcher._is_initialized = True
        dispatcher._channel = None
        legacy.emit("reconnected")
        await tick(10)
        assert dispatcher.channel is not None


class TestAReconnectionIsOneLineage:
    async def test_does_not_fork_into_two_connections_when_the_socket_drops_during_restoration(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", ReconnectionOptions(max_retries=8, initial_delay_ms=1, max_delay_ms=5))
        state = {"dropped": False, "announced": 0}
        conn.on("reconnected", lambda: state.__setitem__("announced", state["announced"] + 1))
        conn.on("error", lambda _e: None)

        async def restore(_g):
            if not state["dropped"]:
                state["dropped"] = True
                await tick()
                conn.handle.drop()  # socket dies mid-restore
                await tick()
                raise RuntimeError("channel gone during restore")

        conn.register_restorer(restore)
        handles[0].drop()
        await settle(0.4)
        assert state["announced"] == 1
        assert [h for h in handles[1:] if not h.closed].__len__() == 1
        await conn.disconnect()

    async def test_announces_once_on_a_live_socket_when_the_drop_lands_mid_restore_but_restoration_succeeds(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", ReconnectionOptions(max_retries=8, initial_delay_ms=1, max_delay_ms=5))
        state = {"dropped": False, "announced": 0}
        conn.on("reconnected", lambda: state.__setitem__("announced", state["announced"] + 1))
        conn.on("error", lambda _e: None)

        async def restore(_g):
            if not state["dropped"]:
                state["dropped"] = True
                await tick()
                conn.handle.drop()  # dies mid-restore, but the restorer resolves

        conn.register_restorer(restore)
        handles[0].drop()
        await settle(0.4)
        assert state["announced"] == 1
        assert conn.is_ready is True
        assert conn.is_reconnecting is False
        assert conn.handle.closed is False
        assert len([h for h in handles[1:] if not h.closed]) == 1
        await conn.disconnect()

    async def test_keeps_a_stopped_then_restarted_listener_taking_part_in_restoration(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        conn.on("error", lambda _e: None)
        listener = ProbeListener(conn)

        async def handler(*_a):
            return None

        await listener.init(handler, "probe.queue")
        await listener.start()
        await listener.stop_consuming()
        await listener.start()

        handles[0].drop()
        await settle()
        assert listener.restores == 1
        await conn.disconnect()

    async def test_a_stopped_listener_is_not_restored(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        listener = ProbeListener(conn)

        async def handler(*_a):
            return None

        await listener.init(handler, "probe.queue")
        await listener.start()
        await listener.stop_consuming()
        handles[0].drop()
        await settle()
        assert listener.restores == 0
        await conn.disconnect()

    async def test_leaks_no_handler_count_when_a_non_async_handler_raises_synchronously(self):
        from protobus.connection import ConsumeOptions

        from ..helpers import FakeChannel, make_delivery

        conn = Connection()
        ch = FakeChannel()

        def handler(*_a):
            raise RuntimeError("sync boom")

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True)
        await ch.deliver(make_delivery())
        assert conn.in_flight_deliveries == 0
        assert await conn.drain_in_flight(50) is True


class TestConnectionLifecycle:
    async def test_is_single_flight(self, handles):
        conn = Connection()
        a, b, c = await asyncio.gather(
            conn.connect("amqp://localhost"), conn.connect("amqp://localhost"), conn.connect("amqp://localhost")
        )
        assert len(handles) == 1
        assert a is b is c is handles[0]
        await conn.disconnect()

    async def test_refuses_to_connect_twice(self, handles):
        from protobus import AlreadyConnectedError

        conn = Connection()
        await conn.connect("amqp://localhost")
        with pytest.raises(AlreadyConnectedError):
            await conn.connect("amqp://localhost")
        await conn.disconnect()

    async def test_does_not_come_back_up_when_disconnect_races_an_in_flight_reconnect(self, handles):
        first = FakeHandle()
        late = FakeHandle()
        second_connect = asyncio.get_event_loop().create_future()
        handles.behaviours.extend([first, lambda: second_connect])

        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        first.drop()
        await asyncio.sleep(0.05)
        assert len(handles) == 1  # the reconnect attempt is in flight

        await conn.disconnect()
        second_connect.set_result(late)
        await tick(5)

        assert conn.is_connected is False
        assert conn.is_reconnecting is False
        assert late.closed is True

    async def test_reports_the_real_number_of_reconnection_attempts(self, handles):
        log = CapturingLogger()
        set_logger(log)
        set_log_level(LogLevel.Info)
        first = FakeHandle()
        revived = FakeHandle()
        handles.behaviours.extend([first, RuntimeError("still down"), revived])

        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        first.drop()
        await settle(0.3)
        assert "reconnection successful after 2 attempts" in log.text
        assert "after 0 attempts" not in log.text
        await conn.disconnect()

    async def test_a_reconnected_generation_is_a_different_handle(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        first = conn.handle
        generation = conn.generation
        first.drop()
        await settle()
        assert conn.handle is not first
        assert conn.generation > generation
        assert conn.is_connected
        await conn.disconnect()

    async def test_disconnect_is_manual_and_does_not_reconnect(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        events = []
        conn.on("disconnected", lambda: events.append("disconnected"))
        conn.on("reconnecting", lambda _i: events.append("reconnecting"))
        await conn.disconnect()
        await settle(0.05)
        assert events == []
        assert len(handles) == 1
        assert handles[0].closed

    async def test_a_dropped_socket_emits_disconnected_then_reconnecting(self, handles):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        events = []
        conn.on("disconnected", lambda: events.append("disconnected"))
        conn.on("reconnecting", lambda info: events.append(("reconnecting", info["attempt"])))
        conn.on("reconnected", lambda: events.append("reconnected"))
        handles[0].drop()
        await settle()
        assert events == ["disconnected", ("reconnecting", 1), "reconnected"]
        await conn.disconnect()
