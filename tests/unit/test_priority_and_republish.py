"""Message priority (opt-in, byte-identical when unset), and the properties a
republish carries onto the retry and DLQ hops."""

import asyncio

import pytest
from pamqp.commands import Basic

from protobus import (
    Config,
    Connection,
    EventListener,
    InvalidPriorityError,
    MessageDispatcher,
    MessageFactory,
    MessageListener,
    MessageService,
    MessageServiceOptions,
    RetryOptions,
)
from protobus.connection import RESPONSE_BUFFER_ATTR, ConsumeOptions, ConsumeRetryOptions

from ..helpers import FakeChannel, FakeConnection, FakeContext, make_delivery, tick

NO_RETRY = RetryOptions(max_retries=0, retry_delay_ms=5000)
RETRY = ConsumeRetryOptions(max_retries=2, retry_queue_name="Svc.Retry", retry_exchange_name="Svc.Retry.Exchange", dlq_name="Svc.DLQ")


async def noop(*_a):
    return None


def args_for(conn, name):
    hits = [q for q in conn.declared_queues if q["queue"] == name]
    return hits[-1]["options"]["arguments"] if hits else None


class TestQueueDeclarationIsUnchangedUnlessMaxPriorityIsAskedFor:
    async def test_declares_an_empty_arguments_object_when_max_priority_is_not_set(self):
        conn = FakeConnection()
        await MessageListener(conn, True, 1, NO_RETRY).init(noop, "Svc")
        assert args_for(conn, "Svc") == {}

    async def test_leaves_a_ttl_only_arguments_object_alone_when_max_priority_is_not_set(self):
        conn = FakeConnection()
        await MessageListener(conn, True, 1, RetryOptions(0, 5000, 60000)).init(noop, "Svc")
        assert args_for(conn, "Svc") == {"x-message-ttl": 60000}

    async def test_declares_x_max_priority_when_max_priority_is_set(self):
        conn = FakeConnection()
        await MessageListener(conn, True, 1, NO_RETRY, None, 2).init(noop, "Svc")
        assert args_for(conn, "Svc") == {"x-max-priority": 2}

    async def test_declares_x_max_priority_alongside_a_message_ttl(self):
        conn = FakeConnection()
        await MessageListener(conn, True, 1, RetryOptions(0, 5000, 60000), None, 2).init(noop, "Svc")
        assert args_for(conn, "Svc") == {"x-message-ttl": 60000, "x-max-priority": 2}

    async def test_declares_the_same_arguments_on_the_reconnection_path(self):
        for max_priority, expected in ((2, {"x-max-priority": 2}), (None, {})):
            conn = FakeConnection()
            listener = MessageListener(conn, True, 1, NO_RETRY, None, max_priority)
            await listener.init(noop, "Svc")
            conn.declared_queues.clear()
            await listener._reinitialize()
            assert args_for(conn, "Svc") == expected

    async def test_leaves_the_retry_queue_and_dlq_arguments_untouched(self):
        conn = FakeConnection()
        listener = MessageListener(conn, True, 1, RetryOptions(3, 5000), None, 2)
        await listener.init(noop, "Svc")
        await listener.subscribe("REQUEST.Svc.*")
        assert args_for(conn, "Svc.DLQ") == {}
        assert args_for(conn, "Svc.Retry") == {"x-message-ttl": 5000, "x-dead-letter-exchange": Config.bus_exchange_name()}

    async def test_the_retry_topology_is_declared_and_bound(self):
        conn = FakeConnection()
        listener = MessageListener(conn, True, 1, RetryOptions(3, 5000))
        await listener.init(noop, "Svc")
        await listener.subscribe("REQUEST.Svc.*")
        assert {"queue": "Svc", "exchange": "proto.bus", "routing_key": "REQUEST.Svc.*"} in conn.bindings
        assert {"queue": "Svc.Retry", "exchange": "Svc.Retry.Exchange", "routing_key": "#"} in conn.bindings
        assert any(e["exchange"] == "Svc.Retry.Exchange" and e["type"] == "topic" for e in conn.declared_exchanges)
        opts = listener.get_retry_options()
        assert (opts.max_retries, opts.retry_queue_name, opts.retry_exchange_name, opts.dlq_name) == (3, "Svc.Retry", "Svc.Retry.Exchange", "Svc.DLQ")
        await listener.start()
        assert conn.consumes[-1]["retry_options"] is opts or conn.consumes[-1]["retry_options"].dlq_name == "Svc.DLQ"

    async def test_no_retry_topology_when_retries_are_disabled(self):
        conn = FakeConnection()
        listener = MessageListener(conn, True, 1, NO_RETRY)
        await listener.init(noop, "Svc")
        await listener.subscribe("REQUEST.Svc.*")
        assert [q["queue"] for q in conn.declared_queues] == ["Svc"]
        assert listener.get_retry_options() is None
        await listener.start()
        assert conn.consumes[-1]["retry_options"] is None

    async def test_a_changed_retry_delay_is_reported_as_a_mismatch(self):
        from protobus import RetryQueueMismatchError

        conn = FakeConnection()
        original = conn.declare_queue

        async def declare(channel, name, options=None):
            if name == "Svc.Retry":
                raise RuntimeError("PRECONDITION_FAILED - inequivalent arg 'x-message-ttl'")
            return await original(channel, name, options)

        conn.declare_queue = declare  # type: ignore[method-assign]
        listener = MessageListener(conn, True, 1, RetryOptions(3, 7000))
        await listener.init(noop, "Svc")
        with pytest.raises(RetryQueueMismatchError, match="7000ms"):
            await listener.subscribe("REQUEST.Svc.*")


class TestMaxPriorityValidation:
    @pytest.mark.parametrize("value", [0, -1, 256, 1.5, "2", float("nan"), True])
    def test_rejects(self, value):
        with pytest.raises(InvalidPriorityError):
            MessageListener(FakeConnection(), True, 1, NO_RETRY, None, value)

    @pytest.mark.parametrize("value", [1, 2, 10, 255])
    def test_accepts(self, value):
        MessageListener(FakeConnection(), True, 1, NO_RETRY, None, value)

    def test_accepts_none(self):
        MessageListener(FakeConnection(), True, 1, NO_RETRY, None, None)


class TestPerMessagePriorityOnThePublishPath:
    async def test_sets_no_priority_property_at_all_when_none_is_asked_for(self):
        conn = FakeConnection()
        d = MessageDispatcher(conn)
        await d.init()
        await d.publish(b"x", "REQUEST.A.B.c", False)
        assert "priority" not in conn.publishes[0]["properties"]

    async def test_sets_the_priority_property_on_a_fire_and_forget_publish(self):
        from protobus import CallOptions

        conn = FakeConnection()
        d = MessageDispatcher(conn)
        await d.init()
        await d.publish(b"x", "REQUEST.A.B.c", False, None, CallOptions(priority=2))
        assert conn.publishes[0]["properties"]["priority"] == 2

    async def test_sets_the_priority_property_on_an_rpc_publish(self):
        from protobus import CallOptions

        conn = FakeConnection()
        d = MessageDispatcher(conn)
        await d.init()
        pending = asyncio.ensure_future(d.publish(b"x", "REQUEST.A.B.c", True, 50, CallOptions(priority=2)))
        await asyncio.sleep(0.025)
        assert conn.publishes[0]["properties"]["priority"] == 2
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    async def test_sends_an_explicit_priority_of_0_rather_than_treating_it_as_unset(self):
        from protobus import CallOptions

        conn = FakeConnection()
        d = MessageDispatcher(conn)
        await d.init()
        await d.publish(b"x", "REQUEST.A.B.c", False, None, CallOptions(priority=Config.PRIORITY_NORMAL))
        assert conn.publishes[0]["properties"]["priority"] == 0

    async def test_the_legacy_priority_keyword_still_works(self):
        conn = FakeConnection()
        d = MessageDispatcher(conn)
        await d.init()
        await d.publish(b"x", "REQUEST.A.B.c", False, priority=1)
        assert conn.publishes[0]["properties"]["priority"] == 1


class TestPerMessagePriorityValidation:
    async def publish_with(self, priority):
        from protobus import CallOptions

        conn = FakeConnection()
        d = MessageDispatcher(conn)
        await d.init()
        return await d.publish(b"x", "REQUEST.A.B.c", False, None, CallOptions(priority=priority))

    @pytest.mark.parametrize("value", [-1, 256, 1.5, "high", float("nan"), True])
    async def test_rejects(self, value):
        with pytest.raises(InvalidPriorityError):
            await self.publish_with(value)

    @pytest.mark.parametrize("value", [0, 1, 2, 255])
    async def test_accepts(self, value):
        assert await self.publish_with(value) is None


class TestMessageServiceThreadsMaxPriorityToItsRequestListener:
    class Svc(MessageService):
        service_name = "Test.Svc"
        proto_file_name = "unused.proto"

    def test_passes_max_priority_through_to_the_listener(self):
        svc = self.Svc(FakeContext(MessageFactory()), MessageServiceOptions(max_priority=2))
        assert svc.listener._max_priority == 2

    def test_leaves_the_listener_without_a_max_priority_when_the_option_is_omitted(self):
        svc = self.Svc(FakeContext(MessageFactory()))
        assert svc.listener._max_priority is None

    def test_rejects_an_out_of_range_max_priority_at_construction(self):
        with pytest.raises(InvalidPriorityError):
            self.Svc(FakeContext(MessageFactory()), MessageServiceOptions(max_priority=256))

    def test_does_not_give_the_events_listener_a_priority_queue(self):
        svc = self.Svc(FakeContext(MessageFactory()), MessageServiceOptions(max_priority=2))
        assert svc.event_listener._max_priority is None

    def test_rejects_max_priority_combined_with_late_ack_false(self):
        with pytest.raises(InvalidPriorityError):
            self.Svc(FakeContext(MessageFactory()), MessageServiceOptions(max_priority=2, late_ack=False))

    def test_allows_max_priority_on_the_default_late_ack_path(self):
        self.Svc(FakeContext(MessageFactory()), MessageServiceOptions(max_priority=2))

    def test_still_allows_late_ack_false_when_no_priority_is_asked_for(self):
        svc = self.Svc(FakeContext(MessageFactory()), MessageServiceOptions(late_ack=False))
        assert svc.listener._late_ack is False

    def test_keyword_options_are_accepted(self):
        svc = self.Svc(FakeContext(MessageFactory()), max_concurrent=4, max_priority=2)
        assert svc.listener._max_concurrent == 4 and svc.listener._max_priority == 2

    def test_late_ack_is_the_default_with_a_bounded_prefetch(self):
        svc = self.Svc(FakeContext(MessageFactory()))
        assert svc.listener._late_ack is True
        assert svc.listener.effective_prefetch() == 1

    async def test_applies_a_bounded_prefetch_on_the_late_ack_path(self):
        conn = FakeConnection()
        listener = MessageListener(conn, True, None, RetryOptions(0, 1), None, 2)
        await listener.init(noop, "Svc")
        assert conn.prefetches == [Config.default_prefetch()]


def priority_message(priority, retry_count=0):
    return make_delivery(
        reply_to=None, message_id="mid-1", priority=priority,
        headers={"x-retry-count": retry_count} if retry_count else {},
    )


class TestARepublishedMessageKeepsItsPriority:
    async def deliver(self, retry, message):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            raise RuntimeError("boom")

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, retry)
        await ch.deliver(message)
        return ch

    async def test_carries_the_priority_onto_the_retry_exchange(self):
        ch = await self.deliver(RETRY, priority_message(Config.PRIORITY_CONTROL))
        assert ch.published_to("Svc.Retry.Exchange")[0].properties.priority == Config.PRIORITY_CONTROL

    async def test_carries_the_priority_onto_the_retry_queue_on_the_no_exchange_fallback(self):
        no_exchange = ConsumeRetryOptions(**{**RETRY.__dict__, "retry_exchange_name": None})
        ch = await self.deliver(no_exchange, priority_message(Config.PRIORITY_CONTROL))
        assert ch.sent_to_queue("Svc.Retry")[0].properties.priority == Config.PRIORITY_CONTROL

    async def test_carries_the_priority_onto_the_dlq_once_retries_are_exhausted(self):
        ch = await self.deliver(RETRY, priority_message(Config.PRIORITY_CONTROL, RETRY.max_retries))
        assert ch.sent_to_queue("Svc.DLQ")[0].properties.priority == Config.PRIORITY_CONTROL

    async def test_sets_no_priority_on_the_retry_hop_when_the_original_had_none(self):
        ch = await self.deliver(RETRY, priority_message(None))
        assert ch.published_to("Svc.Retry.Exchange")[0].properties.priority is None


ORIGINAL_PROPERTIES = {
    "content_type": "application/octet-stream",
    "content_encoding": "gzip",
    "correlation_id": "cid-1",
    "message_id": "mid-1",
    "reply_to": "amq.gen-callback",
    "delivery_mode": 2,
    "priority": 7,
    "timestamp": None,  # filled in below: pamqp carries a datetime
    "message_type": "Svc.Api.Request",
    "app_id": "orders-api",
    "user_id": "guest",
    "expiration": "60000",
}

# Properties a republish must NOT copy, each for a reason about the hop.
DELIBERATELY_DROPPED = {
    "delivery_mode": "re-expressed as persistent at every site",
    "expiration": "would race the retry queue's TTL, or delete DLQ evidence",
    "user_id": "validated by the broker against the publishing connection",
}
DROPPED_ON_DLQ = {"reply_to": "the caller has already been answered on the error-reply path"}


def full_message(retry_count=0):
    from datetime import datetime, timezone

    props = dict(ORIGINAL_PROPERTIES)
    props["timestamp"] = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    headers = {"x-tenant": "acme"}
    if retry_count:
        headers["x-retry-count"] = retry_count
    return make_delivery(headers=headers, **{k: v for k, v in props.items()})


class TestTheRepublishPropertySetIsAuditedAsAWhole:
    async def deliver(self, retry_count=0, retry=RETRY):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            raise RuntimeError("boom")

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, retry)
        await ch.deliver(full_message(retry_count))
        return ch

    def expected(self):
        out = dict(ORIGINAL_PROPERTIES)
        from datetime import datetime, timezone

        out["timestamp"] = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
        return out

    async def test_the_retry_hop_carries_every_property_that_is_not_deliberately_dropped(self):
        ch = await self.deliver()
        hop = ch.published_to("Svc.Retry.Exchange")[0].properties
        missing = [k for k, v in self.expected().items() if k not in DELIBERATELY_DROPPED and getattr(hop, k) != v]
        assert missing == []

    async def test_the_dlq_hop_carries_every_property_that_is_not_deliberately_dropped(self):
        ch = await self.deliver(RETRY.max_retries)
        hop = ch.sent_to_queue("Svc.DLQ")[0].properties
        missing = [k for k, v in self.expected().items() if k not in DELIBERATELY_DROPPED and k not in DROPPED_ON_DLQ and getattr(hop, k) != v]
        assert missing == []
        assert hop.reply_to is None

    async def test_drops_exactly_the_properties_it_means_to_and_no_others(self):
        ch = await self.deliver()
        hop = ch.published_to("Svc.Retry.Exchange")[0].properties
        assert hop.expiration is None
        assert hop.user_id is None
        # Persistence is preserved, just spelled the other way.
        assert hop.delivery_mode == 2

    async def test_gains_no_property_the_original_did_not_carry(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            raise RuntimeError("boom")

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, RETRY)
        await ch.deliver(make_delivery(reply_to=None, message_id=None))
        hop = ch.published_to("Svc.Retry.Exchange")[0].properties
        for key in ("content_type", "content_encoding", "priority", "timestamp", "message_type", "app_id"):
            assert getattr(hop, key) is None, key

    async def test_carries_content_type_on_every_hop(self):
        ch = await self.deliver()
        assert ch.published_to("Svc.Retry.Exchange")[0].properties.content_type == "application/octet-stream"
        no_exchange = ConsumeRetryOptions(**{**RETRY.__dict__, "retry_exchange_name": None})
        ch = await self.deliver(0, no_exchange)
        assert ch.sent_to_queue("Svc.Retry")[0].properties.content_type == "application/octet-stream"
        ch = await self.deliver(RETRY.max_retries)
        assert ch.sent_to_queue("Svc.DLQ")[0].properties.content_type == "application/octet-stream"


class TestTerminalSettlementWhenTheErrorReplyCannotBePublished:
    async def deliver_with_broken_reply(self, retry, retry_count):
        conn = Connection()
        ch = FakeChannel()
        original = ch.basic_publish

        def failing(body, *, exchange="", routing_key="", **kw):
            future = original(body, exchange=exchange, routing_key=routing_key, **kw)
            if exchange == Config.callbacks_exchange_name():
                import aiormq

                record = ch.published[-1]
                record.future = asyncio.get_event_loop().create_future()
                record.future.set_exception(aiormq.exceptions.DeliveryError(None, Basic.Nack(delivery_tag=1)))
                return record.future
            return future

        ch.basic_publish = failing  # type: ignore[method-assign]

        async def handler(*_a):
            err = RuntimeError("permanent failure")
            setattr(err, RESPONSE_BUFFER_ATTR, b"encoded-error-response")
            raise err

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, retry)
        await ch.deliver(make_delivery(headers={"x-retry-count": retry_count} if retry_count else {}))
        return ch

    async def test_still_dead_letters_the_message_once_retries_are_exhausted(self):
        ch = await self.deliver_with_broken_reply(RETRY, RETRY.max_retries)
        assert len(ch.sent_to_queue("Svc.DLQ")) == 1
        assert len(ch.acked) == 1

    async def test_still_rejects_it_when_no_retry_is_configured(self):
        ch = await self.deliver_with_broken_reply(None, 0)
        assert len(ch.rejected) == 1


class TestMessageIdentitySurvivesThePathsThatDuplicate:
    async def test_carries_message_id_onto_the_retry_and_dlq_publishes(self):
        for retry_count, where in ((0, "retry"), (2, "dlq")):
            conn = Connection()
            ch = FakeChannel()

            async def handler(*_a):
                raise RuntimeError("boom")

            await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, RETRY)
            await ch.deliver(make_delivery(message_id="stable-id-1", headers={"x-retry-count": retry_count} if retry_count else {}))
            hop = ch.published_to("Svc.Retry.Exchange")[0] if where == "retry" else ch.sent_to_queue("Svc.DLQ")[0]
            assert hop.properties.message_id == "stable-id-1"

    async def test_gives_the_handler_the_identity_it_needs_to_deduplicate(self):
        conn = Connection()
        ch = FakeChannel()
        seen = {}

        async def handler(_c, _id, _h, context):
            seen["context"] = context

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, RETRY)
        await ch.deliver(make_delivery(message_id="stable-id-1", redelivered=True))
        assert seen["context"].message_id == "stable-id-1"
        assert seen["context"].redelivered is True
