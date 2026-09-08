"""Graceful shutdown: drain accounting, ordering, and a SIGTERM that lands
while the broker is away."""

import asyncio
from typing import List

import pytest

import protobus.connection as connection_module
from protobus import BaseListener, Connection, ReconnectionOptions, RunnableService
from protobus.connection import ConsumeOptions

from ..helpers import FakeChannel, FakeHandle, make_delivery, tick

FAST = ReconnectionOptions(max_retries=5, initial_delay_ms=1, max_delay_ms=5)


class TestInFlightDeliveryTracking:
    async def test_counts_a_delivery_for_as_long_as_its_handler_is_running(self):
        conn = Connection()
        ch = FakeChannel()
        gate = asyncio.Event()

        async def handler(*_a):
            await gate.wait()

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        assert conn.in_flight_deliveries == 0
        delivery = asyncio.ensure_future(ch.deliver(make_delivery(correlation_id="cid-1")))
        await tick(2)
        assert conn.in_flight_deliveries == 1
        gate.set()
        await delivery
        assert conn.in_flight_deliveries == 0

    async def test_drains_once_in_flight_handlers_finish(self):
        conn = Connection()
        ch = FakeChannel()
        gate = asyncio.Event()

        async def handler(*_a):
            await gate.wait()

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        delivery = asyncio.ensure_future(ch.deliver(make_delivery(correlation_id="cid-2")))
        await tick(2)
        drain = asyncio.ensure_future(conn.drain_in_flight(5000))
        await tick(2)
        assert not drain.done()
        gate.set()
        await delivery
        assert await drain is True

    async def test_gives_up_draining_at_the_deadline_rather_than_hanging_shutdown(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            await asyncio.Event().wait()  # never

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        asyncio.ensure_future(ch.deliver(make_delivery(correlation_id="cid-3")))
        await tick(2)
        assert await conn.drain_in_flight(30) is False
        assert conn.in_flight_deliveries == 1

    async def test_resolves_immediately_when_nothing_is_in_flight(self):
        assert await Connection().drain_in_flight(5000) is True

    async def test_does_not_report_drained_while_a_timed_out_handler_is_still_running(self):
        conn = Connection()
        ch = FakeChannel()
        release = asyncio.Event()
        state = {"done": False}

        async def handler(*_a):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass
            await release.wait()  # ignores its cancellation and runs on
            state["done"] = True

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, None, 20)
        asyncio.ensure_future(ch.deliver(make_delivery()))
        await asyncio.sleep(0.12)
        # The delivery has been settled and rejected by now...
        assert ch.rejected
        drained = await conn.drain_in_flight(80)
        assert state["done"] is False
        # ...but the handler has not returned, so a drain must not claim it has.
        assert drained is False
        release.set()
        assert await conn.drain_in_flight(1000) is True


class TestPublishBookkeepingSurvivesAChannelTeardown:
    async def test_does_not_drive_the_outstanding_confirm_counter_negative(self):
        conn = Connection()
        ch = FakeChannel(auto_confirm=False)
        state = conn._publish_state_for(ch)
        p1 = asyncio.ensure_future(conn.publish(ch, "ex", "rk", b"1", {}))
        p2 = asyncio.ensure_future(conn.publish(ch, "ex", "rk", b"1", {}))
        await tick(3)
        assert state.in_flight == 2
        ch.emit_close()
        await asyncio.gather(p1, p2, return_exceptions=True)
        assert state.in_flight == 0


class ProbeListener(BaseListener):
    def __init__(self, connection):
        super().__init__(connection)
        self._exchange_name = "probe.exchange"
        self._exchange_type = "topic"

    @property
    def restorer_attached(self):
        return self._restorer_attached


@pytest.fixture
def gated_broker(monkeypatch):
    """Connect once immediately; hold every reconnection behind a barrier."""
    gate = asyncio.Event()
    state = {"calls": 0, "handles": [], "release": gate.set}

    async def connect(url, *a, **kw):
        state["calls"] += 1
        if state["calls"] > 1:
            await gate.wait()
        h = FakeHandle()
        state["handles"].append(h)
        return h

    monkeypatch.setattr(connection_module.aiormq, "connect", connect)
    return state


async def noop(*_a):
    return None


class TestGracefulShutdownWhileTheBrokerIsAway:
    async def test_does_not_resume_consuming_when_the_broker_comes_back_mid_shutdown(self, gated_broker):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        listener = ProbeListener(conn)
        await listener.init(noop, "probe.queue")
        await listener.start()
        consumes: List[str] = []
        original = conn.consume

        async def counting(*a, **kw):
            consumes.append(a[1])
            return await original(*a, **kw)

        conn.consume = counting  # type: ignore[method-assign]
        gated_broker["handles"][0].drop()
        await tick(2)
        # The disconnect drops the tag but keeps the listener enrolled.
        assert listener.consumer_tag == ""
        assert listener.was_started is True

        # SIGTERM lands here, with the broker still away.
        await listener.stop_consuming()
        gated_broker["release"]()
        await asyncio.sleep(0.2)

        assert consumes == []
        assert listener.was_started is False
        assert listener.restorer_attached is False
        await conn.disconnect()

    async def test_still_cancels_the_broker_side_consumer_when_it_is_connected(self, gated_broker):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        listener = ProbeListener(conn)
        await listener.init(noop, "probe.queue")
        await listener.start()
        tag = listener.consumer_tag
        assert tag
        cancels = []
        original = conn.cancel

        async def spy(channel, consumer_tag):
            cancels.append(consumer_tag)
            return await original(channel, consumer_tag)

        conn.cancel = spy  # type: ignore[method-assign]
        await listener.stop_consuming()
        assert cancels == [tag]
        await conn.disconnect()

    async def test_is_safe_to_call_more_than_once_and_while_disconnected(self, gated_broker):
        conn = Connection()
        await conn.connect("amqp://localhost", FAST)
        listener = ProbeListener(conn)
        await listener.init(noop, "probe.queue")
        await listener.start()
        cancels = []
        original = conn.cancel

        async def spy(channel, consumer_tag):
            cancels.append(consumer_tag)
            return await original(channel, consumer_tag)

        conn.cancel = spy  # type: ignore[method-assign]
        await listener.stop_consuming()
        await listener.stop_consuming()
        gated_broker["handles"][0].drop()
        await tick(2)
        await listener.stop_consuming()
        assert len(cancels) == 1
        await conn.disconnect()


class TestShutdownOrdering:
    async def test_stops_intake_and_drains_before_running_user_cleanup(self):
        order = []

        class Conn:
            is_connected = True
            in_flight_deliveries = 1

            async def drain_in_flight(self, _ms):
                order.append("drain")
                return True

            async def disconnect(self):
                order.append("disconnect")

        class Ctx:
            connection = Conn()
            factory = None

        class Svc(RunnableService):
            service_name = "T.Service"

            def __init__(self, context, options=None, **kw):
                # Skip listener construction: the fake context has no bus.
                self.context = context
                self._shutdown_event = None
                self._shutting_down = False

            async def init(self):
                order.append("init")

            async def stop_consuming(self):
                order.append("stop_consuming")

            async def cleanup(self):
                order.append("cleanup")

        service = await Svc.launch(Ctx(), Svc)
        run = asyncio.ensure_future(service.run())
        await tick(2)
        service.request_shutdown("test")
        assert await run == 0
        assert order == ["init", "stop_consuming", "drain", "cleanup", "disconnect"]

    async def test_a_startup_failure_shuts_down_with_exit_code_1(self):
        order = []

        class Conn:
            is_connected = True
            in_flight_deliveries = 0

            async def disconnect(self):
                order.append("disconnect")

        class Ctx:
            connection = Conn()
            factory = None

        class Svc(RunnableService):
            service_name = "T.Service"

            def __init__(self, context, options=None, **kw):
                self.context = context
                self._shutdown_event = None
                self._shutting_down = False

            async def init(self):
                raise RuntimeError("no broker")

            async def stop_consuming(self):
                order.append("stop_consuming")

            async def cleanup(self):
                order.append("cleanup")

        with pytest.raises(RuntimeError, match="no broker"):
            await Svc.launch(Ctx(), Svc)
        assert order == ["stop_consuming", "cleanup", "disconnect"]

    async def test_the_drain_budget_is_configurable(self, monkeypatch):
        monkeypatch.setenv("SHUTDOWN_DRAIN_TIMEOUT_MS", "123")
        seen = {}

        class Conn:
            is_connected = True
            in_flight_deliveries = 2

            async def drain_in_flight(self, ms):
                seen["budget"] = ms
                return False

            async def disconnect(self):
                pass

        class Ctx:
            connection = Conn()
            factory = None

        class Svc(RunnableService):
            service_name = "T.Service"

            def __init__(self, context, options=None, **kw):
                self.context = context
                self._shutdown_event = None
                self._shutting_down = False

            async def stop_consuming(self):
                pass

        await Svc(Ctx()).shutdown("test")
        assert seen["budget"] == 123
