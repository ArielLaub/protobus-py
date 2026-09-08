"""
The consume/settle ladder in connection.py, exercised without a broker:
retry, DLQ, handled errors, early ack, the processing timeout, and dispatch
honouring the broker routing key.
"""

import asyncio

import pytest

from protobus import Connection, HandledError, MessageService
from protobus.connection import RESPONSE_BUFFER_ATTR, ConsumeOptions, ConsumeRetryOptions

from ..helpers import FakeChannel, FakeConnection, FakeContext, make_delivery, make_factory

RETRY = ConsumeRetryOptions(
    max_retries=2,
    retry_queue_name="Svc.Retry",
    retry_exchange_name="Svc.Retry.Exchange",
    dlq_name="Svc.DLQ",
)


def with_reply(err: BaseException, reply: bytes) -> BaseException:
    setattr(err, RESPONSE_BUFFER_ATTR, reply)
    return err


class TestHandlerErrorsOnTheLateAckPath:
    async def test_retries_an_unhandled_error_rather_than_dropping_the_message(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            raise RuntimeError("boom")

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, RETRY)
        await ch.deliver(make_delivery())

        assert [p.exchange for p in ch.published] == ["Svc.Retry.Exchange"]
        assert len(ch.acked) == 1

    async def test_retry_hop_keeps_the_routing_key_and_the_identity(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            raise RuntimeError("boom")

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, RETRY)
        await ch.deliver(make_delivery(routing_key="REQUEST.Svc.Api.doThing", message_id="stable"))

        hop = ch.published[0]
        assert hop.routing_key == "REQUEST.Svc.Api.doThing"
        assert hop.properties.message_id == "stable"
        assert hop.properties.delivery_mode == 2
        assert hop.properties.reply_to == "callback.queue"
        assert hop.headers["x-retry-count"] == 1
        assert hop.headers["x-original-routing-key"] == "REQUEST.Svc.Api.doThing"
        assert "x-first-failure-time" in hop.headers
        # The class name only: an exception message may quote the payload.
        assert hop.headers["x-last-error"] == "RuntimeError"

    async def test_publishes_the_pre_encoded_error_reply_once_retries_are_exhausted(self):
        conn = Connection()
        ch = FakeChannel()
        error_reply = b"encoded-error"

        async def handler(*_a):
            raise with_reply(RuntimeError("still broken"), error_reply)

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, RETRY)
        await ch.deliver(make_delivery(headers={"x-retry-count": 2}))

        assert any(p.content == error_reply for p in ch.published)
        dlq = ch.sent_to_queue("Svc.DLQ")
        assert len(dlq) == 1
        assert dlq[0].headers["x-original-queue"] == "Svc"
        assert "x-dlq-time" in dlq[0].headers
        assert len(ch.acked) == 1

    async def test_does_not_retry_a_handled_error_and_replies_to_the_caller(self):
        conn = Connection()
        ch = FakeChannel()
        error_reply = b"validation-failed"

        async def handler(*_a):
            raise with_reply(HandledError("bad input", "VALIDATION"), error_reply)

        retry = ConsumeRetryOptions(**{**RETRY.__dict__, "is_handled_error": lambda e: getattr(e, "is_handled", False)})
        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, retry)
        await ch.deliver(make_delivery())

        assert any(p.content == error_reply for p in ch.published)
        assert not any(p.exchange == "Svc.Retry.Exchange" for p in ch.published)
        assert ch.rejected == [{"delivery_tag": 1, "requeue": False}]

    async def test_without_retry_configured_the_message_is_rejected_after_the_reply(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            raise with_reply(RuntimeError("boom"), b"reply")

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, None)
        await ch.deliver(make_delivery())

        assert [p.content for p in ch.published] == [b"reply"]
        assert ch.rejected == [{"delivery_tag": 1, "requeue": False}]
        assert ch.acked == []


class TestHandlerErrorsWhenTheCallerAcksEarly:
    async def test_still_replies_to_the_caller_instead_of_leaving_it_to_time_out(self):
        conn = Connection()
        ch = FakeChannel()
        error_reply = b"encoded-error"

        async def handler(*_a):
            raise with_reply(RuntimeError("boom"), error_reply)

        # late_ack False: acked before processing, so retry and DLQ are
        # impossible — but the caller must still learn it failed.
        await conn.consume(ch, "Svc", handler, ConsumeOptions(), False, RETRY)
        await ch.deliver(make_delivery())

        assert any(p.content == error_reply for p in ch.published)
        assert len(ch.acked) == 1
        assert not any(p.exchange == "Svc.Retry.Exchange" for p in ch.published)


class TestMessageProcessingTimeout:
    async def test_rejects_a_handler_that_overruns_the_timeout(self):
        conn = Connection()
        ch = FakeChannel()
        late_reply = b"late"
        saw_abort = asyncio.Event()

        async def handler(_c, _id, _h, context):
            context.signal.add_listener(saw_abort.set)
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                # The framework cancels the coroutine; a handler may run on.
                await asyncio.sleep(0)
                raise
            return late_reply

        retry = ConsumeRetryOptions(**{**RETRY.__dict__, "max_retries": 0})
        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, retry, 30)
        await ch.deliver(make_delivery())

        assert not any(p.content == late_reply for p in ch.published)
        assert saw_abort.is_set()
        # A timeout is not a handled error: with retries off it is rejected.
        assert ch.rejected == [{"delivery_tag": 1, "requeue": False}]

    async def test_a_timed_out_delivery_climbs_the_retry_ladder(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            await asyncio.sleep(0.2)

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, RETRY, 20)
        await ch.deliver(make_delivery())
        assert [p.exchange for p in ch.published] == ["Svc.Retry.Exchange"]
        assert "TimeoutError" in ch.published[0].headers["x-last-error"]

    async def test_does_not_penalise_a_handler_that_finishes_within_the_timeout(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            return b"ok"

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, None, 5000)
        await ch.deliver(make_delivery())

        assert [p.content for p in ch.published] == [b"ok"]
        assert len(ch.acked) == 1

    async def test_uses_the_configured_default_when_no_limit_is_given(self, monkeypatch):
        monkeypatch.setenv("MESSAGE_PROCESSING_TIMEOUT", "20")
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            await asyncio.sleep(0.2)
            return b"late"

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, None)
        await ch.deliver(make_delivery())
        assert ch.published == []
        assert ch.rejected == [{"delivery_tag": 1, "requeue": False}]


class TestReplies:
    async def test_a_unary_reply_goes_to_reply_to_on_the_callbacks_exchange(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            return b"answer"

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, None)
        await ch.deliver(make_delivery(correlation_id="c-9", reply_to="cb.q"))

        reply = ch.published[0]
        assert reply.exchange == "proto.bus.callback"
        assert reply.routing_key == "cb.q"
        assert reply.properties.correlation_id == "c-9"
        assert reply.properties.content_type == "application/octet-stream"
        # Reply first, ack second: the worst case is a redelivered request,
        # never a settled request whose reply was lost.
        assert len(ch.acked) == 1

    async def test_no_reply_is_published_without_reply_to(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            return b"answer"

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, None)
        await ch.deliver(make_delivery(reply_to=None))
        assert ch.published == []
        assert len(ch.acked) == 1

    async def test_a_three_argument_handler_is_still_supported(self):
        conn = Connection()
        ch = FakeChannel()
        seen = []

        async def handler(content, correlation_id, headers):
            seen.append((content, correlation_id, headers))
            return b"ok"

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, None)
        await ch.deliver(make_delivery(body=b"in", correlation_id="c1", headers={"h": 1}))
        assert seen == [(b"in", "c1", {"h": 1})]

    async def test_the_handler_context_carries_delivery_metadata(self):
        conn = Connection()
        ch = FakeChannel()
        seen = {}

        async def handler(_c, _id, _h, context):
            seen["routing_key"] = context.routing_key
            seen["message_id"] = context.message_id
            seen["redelivered"] = context.redelivered
            return None

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, None)
        await ch.deliver(make_delivery(routing_key="REQUEST.Svc.Api.x", message_id="m-1", redelivered=True))
        assert seen == {"routing_key": "REQUEST.Svc.Api.x", "message_id": "m-1", "redelivered": True}

    async def test_a_reply_publish_failure_still_settles_the_message(self):
        """The reply is what may be lost; the DLQ is the only durable record."""
        conn = Connection()
        ch = FakeChannel()

        original = ch.basic_publish

        def failing_publish(body, *, exchange="", routing_key="", **kw):
            future = original(body, exchange=exchange, routing_key=routing_key, **kw)
            if exchange == "proto.bus.callback":
                future.cancel()  # channel went away under the reply
            return future

        ch.basic_publish = failing_publish  # type: ignore[method-assign]

        async def handler(*_a):
            raise with_reply(RuntimeError("boom"), b"err")

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, RETRY)
        await ch.deliver(make_delivery(headers={"x-retry-count": 2}))
        assert len(ch.sent_to_queue("Svc.DLQ")) == 1
        assert len(ch.acked) == 1


class TestInFlightAccounting:
    async def test_counts_a_delivery_until_it_is_settled(self):
        conn = Connection()
        ch = FakeChannel()
        gate = asyncio.Event()

        async def handler(*_a):
            await gate.wait()
            return b"ok"

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, None)
        task = asyncio.ensure_future(ch.deliver(make_delivery()))
        await asyncio.sleep(0.01)
        assert conn.in_flight_deliveries == 1
        assert await conn.drain_in_flight(10) is False
        gate.set()
        await task
        assert conn.in_flight_deliveries == 0
        assert await conn.drain_in_flight(10) is True

    async def test_a_handler_that_outlives_its_timeout_is_still_counted_as_running(self):
        conn = Connection()
        ch = FakeChannel()
        release = asyncio.Event()

        async def handler(*_a):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Swallows the cancellation: keeps running, as sync code would.
                await release.wait()
            return b"late"

        await conn.consume(ch, "Svc", handler, ConsumeOptions(), True, None, 20)
        await ch.deliver(make_delivery())
        # The delivery settled (rejected), but the handler has not returned.
        assert conn.in_flight_deliveries == 1
        release.set()
        await asyncio.sleep(0.01)
        assert conn.in_flight_deliveries == 0


PROTO = """
    syntax = "proto3";
    package Bank;
    message Req { string x = 1; }
    message Res { string y = 1; }
    service Api {
      rpc read (Req) returns (Res);
      rpc deleteAll (Req) returns (Res);
    }
"""


def bank_service():
    f = make_factory(PROTO)
    calls = []

    class Svc(MessageService):
        service_name = "Bank.Api"
        proto_file_name = "Bank.proto"

        async def read(self, *_a):
            calls.append("read")
            return {"y": "read"}

        async def deleteAll(self, *_a):
            calls.append("deleteAll")
            return {"y": "deleted"}

    return Svc(FakeContext(f)), f, calls


def ctx(routing_key):
    from protobus import AbortController, MessageHandlerContext

    return MessageHandlerContext(signal=AbortController().signal, routing_key=routing_key)


class TestDispatchHonoursTheBrokerRoutingKey:
    async def test_runs_the_method_the_routing_key_names(self):
        svc, f, calls = bank_service()
        buf = f.build_request("Bank.Api.read", {"x": "a"}, "alice")
        out = await svc._on_message(buf, "cid", {}, ctx("REQUEST.Bank.Api.read"))
        assert calls == ["read"]
        assert f.decode_response(out).result.data == {"y": "read"}

    async def test_refuses_a_body_method_that_contradicts_the_routing_key(self):
        svc, f, calls = bank_service()
        buf = f.build_request("Bank.Api.deleteAll", {"x": "a"}, "attacker")
        out = await svc._on_message(buf, "cid", {}, ctx("REQUEST.Bank.Api.read"))
        assert calls == []
        decoded = f.decode_response(out)
        assert decoded.error is not None
        assert decoded.error.code == "PROTOCOL_ERROR"
        # Reported against the method the routing key names.
        assert decoded.error.method == "Bank.Api.read"

    async def test_refuses_a_method_belonging_to_a_different_service(self):
        svc, f, calls = bank_service()
        buf = f.build_request("Bank.Api.deleteAll", {"x": "a"}, "attacker")
        out = await svc._on_message(buf, "cid", {}, ctx("REQUEST.Other.Api.deleteAll"))
        assert calls == []
        assert f.decode_response(out).error is not None

    async def test_still_works_when_no_routing_key_is_supplied(self):
        svc, f, calls = bank_service()
        buf = f.build_request("Bank.Api.read", {"x": "a"}, "alice")
        await svc._on_message(buf, "cid", {})
        assert calls == ["read"]


class TestAFailedSettlementDoesNotOccupyTheWorker:
    """The retry/DLQ publish that precedes an ack can fail. The delivery must
    not then sit unacknowledged on an open channel, holding the prefetch."""

    def retry(self):
        return ConsumeRetryOptions(max_retries=2, retry_queue_name="Q.Retry", retry_exchange_name="Q.Retry.Exchange", dlq_name="Q.DLQ")

    async def test_a_retry_publish_that_times_out_returns_the_delivery_to_the_queue(self, monkeypatch):
        import protobus.connection as connection_module

        monkeypatch.setenv("PUBLISH_CONFIRM_TIMEOUT_MS", "5")
        monkeypatch.setattr(connection_module, "SETTLE_FAILURE_REQUEUE_DELAY_S", 0.01)
        conn, ch = Connection(), FakeChannel(auto_confirm=False)

        async def fail(*_a):
            raise RuntimeError("synthetic handler failure")

        await conn.consume(ch, "Q", fail, ConsumeOptions(), True, self.retry())
        await ch.deliver(make_delivery())
        assert ch.acked == []
        assert ch.rejected == [{"delivery_tag": 1, "requeue": True}]
        assert ch.closed is False
        assert conn.in_flight_deliveries == 0

    async def test_a_definite_publish_failure_returns_the_delivery_too(self, monkeypatch):
        import protobus.connection as connection_module

        monkeypatch.setattr(connection_module, "SETTLE_FAILURE_REQUEUE_DELAY_S", 0.01)
        conn, ch = Connection(), FakeChannel(auto_confirm=False)

        async def fail(*_a):
            raise RuntimeError("synthetic handler failure")

        async def nack_the_retry():
            while not ch.published_to("Q.Retry.Exchange"):
                await asyncio.sleep(0)
            ch.published_to("Q.Retry.Exchange")[0].nack()

        await conn.consume(ch, "Q", fail, ConsumeOptions(), True, self.retry())
        nacker = asyncio.ensure_future(nack_the_retry())
        await ch.deliver(make_delivery())
        await nacker
        assert ch.acked == []
        assert [r["requeue"] for r in ch.rejected] == [True]

    async def test_nothing_is_attempted_on_a_channel_that_closed(self, monkeypatch):
        import protobus.connection as connection_module

        monkeypatch.setattr(connection_module, "SETTLE_FAILURE_REQUEUE_DELAY_S", 0.01)
        conn, ch = Connection(), FakeChannel(auto_confirm=False)

        async def fail(*_a):
            raise RuntimeError("synthetic handler failure")

        async def close_under_the_retry():
            while not ch.published_to("Q.Retry.Exchange"):
                await asyncio.sleep(0)
            await ch.close()  # confirms outstanding: ChannelClosedError

        await conn.consume(ch, "Q", fail, ConsumeOptions(), True, self.retry())
        closer = asyncio.ensure_future(close_under_the_retry())
        await ch.deliver(make_delivery())
        await closer
        # The broker requeues on channel close; nothing to settle here.
        assert ch.acked == [] and ch.rejected == []

    async def test_a_retry_copy_that_routes_nowhere_fails_the_settlement(self, monkeypatch):
        # The retry queue is gone. The copy is published mandatory, so the
        # broker returns it; the original is requeued rather than acked into
        # a confirmed-but-unrouted retry.
        import protobus.connection as connection_module

        monkeypatch.setattr(connection_module, "SETTLE_FAILURE_REQUEUE_DELAY_S", 0.01)
        conn, ch = Connection(), FakeChannel(auto_confirm=False)

        async def fail(*_a):
            raise RuntimeError("synthetic handler failure")

        async def return_the_retry():
            while not ch.published_to("Q.Retry.Exchange"):
                await asyncio.sleep(0)
            ch.published_to("Q.Retry.Exchange")[0].return_unroutable()

        await conn.consume(ch, "Q", fail, ConsumeOptions(), True, self.retry())
        returner = asyncio.ensure_future(return_the_retry())
        await ch.deliver(make_delivery())
        await returner
        assert ch.published_to("Q.Retry.Exchange")[0].mandatory is True
        assert ch.acked == []
        assert [r["requeue"] for r in ch.rejected] == [True]
