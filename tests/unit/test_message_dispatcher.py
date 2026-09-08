"""MessageDispatcher: unary deadlines, CallOptions, and the confirm race."""

import asyncio

import pytest

from protobus import (
    CallOptions,
    Config,
    DisconnectedError,
    EventListener,
    InvalidMessageIdError,
    InvalidPriorityError,
    MessageDispatcher,
    MessageFactory,
    MessageListener,
    NotConnectedError,
    RpcTimeoutError,
)

from ..helpers import FakeConnection, tick


async def dispatcher(conn=None):
    conn = conn or FakeConnection()
    d = MessageDispatcher(conn)
    await d.init()
    return d, conn


class TestUnaryRpcCallTimeout:
    async def test_raises_rpc_timeout_error_instead_of_hanging_forever(self):
        d, _ = await dispatcher()
        with pytest.raises(RpcTimeoutError):
            await d.publish(b"x", "REQUEST.A.B.c", True, 40)

    async def test_does_not_leak_the_pending_callback_entry_after_a_timeout(self):
        d, _ = await dispatcher()
        with pytest.raises(RpcTimeoutError):
            await d.publish(b"x", "REQUEST.A.B.c", True, 40)
        assert len(d.pending_callbacks) == 0

    async def test_still_resolves_normally_when_a_reply_arrives_before_the_timeout(self):
        d, _ = await dispatcher()
        pending = asyncio.ensure_future(d.publish(b"x", "REQUEST.A.B.c", True, 5000))
        while not d.pending_callbacks:
            await tick()
        correlation_id = next(iter(d.pending_callbacks))
        await d._on_result(b"reply", correlation_id, {})
        assert await pending == b"reply"
        assert len(d.pending_callbacks) == 0

    async def test_does_not_arm_a_timeout_for_fire_and_forget_publishes(self):
        d, conn = await dispatcher()
        assert await d.publish(b"x", "EVENT.x", False, 10) is None
        assert len(d.pending_callbacks) == 0
        assert conn.publishes[-1]["properties"]["mandatory"] is False
        assert conn.publishes[-1]["properties"]["reply_to"] is None

    async def test_uses_the_configured_default_deadline(self, monkeypatch):
        monkeypatch.setenv("RPC_CALL_TIMEOUT_MS", "30")
        d, _ = await dispatcher()
        with pytest.raises(RpcTimeoutError):
            await d.publish(b"x", "REQUEST.A.B.c", True)

    async def test_requests_are_persistent_mandatory_and_carry_reply_to(self):
        d, conn = await dispatcher()
        with pytest.raises(RpcTimeoutError):
            await d.publish(b"x", "REQUEST.A.B.c", True, 20)
        props = conn.publishes[-1]["properties"]
        assert props["delivery_mode"] == 2
        assert props["mandatory"] is True
        assert props["reply_to"] == d.callback_listener.callback_queue
        assert props["content_type"] == "application/octet-stream"
        assert conn.publishes[-1]["exchange"] == Config.bus_exchange_name()

    async def test_cancelling_the_caller_releases_the_slot(self):
        d, _ = await dispatcher()
        pending = asyncio.ensure_future(d.publish(b"x", "REQUEST.A.B.c", True, 5000))
        while not d.pending_callbacks:
            await tick()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(d.pending_callbacks) == 0

    async def test_refuses_to_publish_when_there_is_no_connection(self):
        d, conn = await dispatcher()
        conn.is_connected = False
        conn.is_reconnecting = False
        with pytest.raises(NotConnectedError):
            await d.publish(b"x", "REQUEST.A.B.c", True, 10)


class SlowConfirmConnection(FakeConnection):
    """publish() resolves only when the test says so, as a broker confirm under load."""

    def __init__(self):
        super().__init__()
        self.confirmed = asyncio.get_event_loop().create_future()
        self.publish_calls = 0

        async def hook(*_a):
            self.publish_calls += 1
            await asyncio.shield(self.confirmed)

        self.publish_hook = hook

    def release_confirm(self):
        if not self.confirmed.done():
            self.confirmed.set_result(None)

    def fail_confirm(self, err):
        if not self.confirmed.done():
            self.confirmed.set_exception(err)


class TestAReplyDeadlineThatExpiresWhileTheConfirmIsInFlight:
    async def test_still_rejects_the_caller_with_the_timeout(self):
        conn = SlowConfirmConnection()
        d, _ = await dispatcher(conn)
        call = asyncio.ensure_future(d.publish(b"x", "REQUEST.A.B.c", True, 20))
        await asyncio.sleep(0.05)
        conn.release_confirm()
        with pytest.raises(RpcTimeoutError):
            await call
        assert len(d.pending_callbacks) == 0

    async def test_reports_the_publish_failure_not_the_deadline_when_both_happen(self):
        conn = SlowConfirmConnection()
        d, _ = await dispatcher(conn)
        call = asyncio.ensure_future(d.publish(b"x", "REQUEST.A.B.c", True, 20))
        await asyncio.sleep(0.05)
        conn.fail_confirm(RuntimeError("NACK"))
        with pytest.raises(RuntimeError, match="NACK"):
            await call
        assert len(d.pending_callbacks) == 0

    async def test_surfaces_a_disconnect_that_lands_while_the_confirm_is_pending(self):
        conn = SlowConfirmConnection()
        d, _ = await dispatcher(conn)
        call = asyncio.ensure_future(d.publish(b"x", "REQUEST.A.B.c", True, 5000))
        await tick(2)
        conn.emit("disconnected")
        conn.release_confirm()
        with pytest.raises(DisconnectedError):
            await call
        assert len(d.pending_callbacks) == 0

    async def test_a_reply_that_beats_the_confirm_is_not_dropped(self):
        conn = SlowConfirmConnection()
        d, _ = await dispatcher(conn)
        call = asyncio.ensure_future(d.publish(b"x", "REQUEST.A.B.c", True, 5000))
        await tick(2)
        # The callback is armed BEFORE the publish resolves.
        correlation_id = next(iter(d.pending_callbacks))
        await d._on_result(b"fast", correlation_id, {})
        conn.release_confirm()
        assert await call == b"fast"


class TestCallOptionsMessageId:
    async def test_puts_the_caller_supplied_id_on_the_published_message(self):
        d, conn = await dispatcher()
        await d.publish(b"body", "REQUEST.Svc.Api.doThing", False, None, CallOptions(message_id="order-4711-attempt-1"))
        assert conn.publishes[-1]["properties"]["message_id"] == "order-4711-attempt-1"

    async def test_lets_a_republish_after_an_ambiguous_outcome_reuse_the_same_id(self):
        d, conn = await dispatcher()
        for _ in range(2):
            await d.publish(b"body", "REQUEST.Svc.Api.doThing", False, None, CallOptions(message_id="order-4711"))
        assert [p["properties"]["message_id"] for p in conn.publishes] == ["order-4711", "order-4711"]

    async def test_sets_no_message_id_property_when_the_caller_supplies_none(self):
        d, conn = await dispatcher()
        await d.publish(b"body", "REQUEST.Svc.Api.doThing", False)
        assert "message_id" not in conn.publishes[-1]["properties"]

    async def test_refuses_an_empty_id_rather_than_silently_minting_a_uuid(self):
        d, conn = await dispatcher()
        with pytest.raises(InvalidMessageIdError, match="message_id"):
            await d.publish(b"body", "REQUEST.Svc.Api.doThing", False, None, CallOptions(message_id="  "))
        assert conn.publishes == []

    async def test_leaves_priority_validation_alone(self):
        d, conn = await dispatcher()
        with pytest.raises(InvalidPriorityError):
            await d.publish(b"body", "REQUEST.Svc.Api.doThing", False, None, CallOptions(priority=999, message_id="x"))
        assert Config.PRIORITY_NORMAL == 0

    async def test_priority_is_absent_from_the_wire_unless_set(self):
        d, conn = await dispatcher()
        await d.publish(b"body", "REQUEST.Svc.Api.doThing", False)
        assert "priority" not in conn.publishes[-1]["properties"]
        await d.publish(b"body", "REQUEST.Svc.Api.doThing", False, None, CallOptions(priority=Config.PRIORITY_NORMAL))
        assert conn.publishes[-1]["properties"]["priority"] == 0

    async def test_refuses_an_id_longer_than_the_amqp_shortstr_limit(self):
        d, conn = await dispatcher()
        with pytest.raises(InvalidMessageIdError, match="255"):
            await d.publish(b"body", "REQUEST.Svc.Api.doThing", False, None, CallOptions(message_id="x" * 256))
        assert conn.publishes == []

    async def test_measures_the_limit_in_bytes_not_characters(self):
        d, _ = await dispatcher()
        with pytest.raises(InvalidMessageIdError, match="255"):
            await d.publish(b"body", "REQUEST.Svc.Api.doThing", False, None, CallOptions(message_id="ש" * 200))

    async def test_accepts_an_id_exactly_at_the_limit(self):
        d, conn = await dispatcher()
        await d.publish(b"body", "REQUEST.Svc.Api.doThing", False, None, CallOptions(message_id="x" * 255))
        assert conn.publishes[-1]["properties"]["message_id"] == "x" * 255


class TestLateAckConsumersBoundTheirPrefetch:
    async def test_event_listener_does_not_request_unlimited_prefetch(self):
        conn = FakeConnection()
        listener = EventListener(conn, MessageFactory())
        await listener.init(None, "Svc.Events")
        assert conn.prefetches and all(isinstance(c, int) and c > 0 for c in conn.prefetches)

    async def test_message_listener_keeps_its_explicit_prefetch(self):
        conn = FakeConnection()
        listener = MessageListener(conn, True, 7)
        await listener.init(None, "Svc")
        assert 7 in conn.prefetches

    async def test_default_prefetch_is_configurable(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_PREFETCH", "5")
        conn = FakeConnection()
        listener = EventListener(conn, MessageFactory())
        await listener.init(None, "Svc.Events")
        assert conn.prefetches == [5]
