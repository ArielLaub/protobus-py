"""Streaming: client-side lifetime, sequencing, backpressure; server-side cancellation."""

import asyncio

import pytest

from protobus import (
    AbortController,
    Config,
    Connection,
    DisconnectedError,
    MessageDispatcher,
    StreamBackpressureError,
    StreamOptions,
    StreamSequenceError,
    StreamTimeoutError,
)
from protobus.connection import ConsumeOptions, ConsumeRetryOptions

from ..helpers import FakeChannel, FakeConnection, make_delivery, tick


async def dispatcher(conn=None):
    conn = conn or FakeConnection()
    d = MessageDispatcher(conn)
    await d.init()
    return d, conn


async def _done():
    return None


def stream_id(d):
    return next(iter(d.pending_streams))


def chunk(seq, body, final=False):
    return body.encode(), {Config.HEADER_SEQ: seq, Config.HEADER_FINAL: final}


class TestStreamingCallLifetime:
    async def test_does_not_leak_a_stream_that_is_never_iterated(self):
        d, _ = await dispatcher()
        for _ in range(50):
            d.publish_streaming(b"", "REQUEST.X.Y.z", 20)
        assert len(d.pending_streams) == 50
        # Nothing ever iterates them; the idle deadline must still fire.
        await asyncio.sleep(0.1)
        assert len(d.pending_streams) == 0

    async def test_releases_the_caller_signal_when_a_stream_ends_normally(self):
        d, _ = await dispatcher()
        ac = AbortController()
        for _ in range(20):
            reply = d.publish_streaming(b"", "REQUEST.X.Y.z", 1000, StreamOptions(signal=ac.signal))
            nxt = asyncio.ensure_future(reply.__anext__())
            await tick(2)
            await d._on_result(b"", stream_id(d), {Config.HEADER_FINAL: True})
            with pytest.raises(StopAsyncIteration):
                await nxt
        assert ac.signal._listeners == []

    async def test_tells_the_producer_to_stop_when_the_stream_times_out_idle(self):
        d, conn = await dispatcher()
        reply = d.publish_streaming(b"", "REQUEST.X.Y.z", 20)
        with pytest.raises(StreamTimeoutError, match="within 20ms"):
            await reply.__anext__()
        await tick(2)
        cancels = [p for p in conn.publishes if p["exchange"] == Config.cancel_exchange_name()]
        assert len(cancels) == 1
        assert cancels[0]["properties"]["correlation_id"] == reply.correlation_id

    async def test_wakes_a_consumer_parked_on_the_next_chunk_when_the_caller_aborts(self):
        d, _ = await dispatcher()
        ac = AbortController()
        reply = d.publish_streaming(b"", "REQUEST.X.Y.z", 60000, StreamOptions(signal=ac.signal))
        pending = asyncio.ensure_future(reply.__anext__())
        await tick(2)
        ac.abort()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending, 0.5)

    async def test_publishes_nothing_for_a_call_whose_signal_is_already_aborted(self):
        d, conn = await dispatcher()
        ac = AbortController()
        ac.abort()
        reply = d.publish_streaming(b"1234", "REQUEST.X.Y.z", 1000, StreamOptions(signal=ac.signal))
        with pytest.raises(StopAsyncIteration):
            await reply.__anext__()
        await tick(2)
        assert conn.publishes == []
        assert len(d.pending_streams) == 0

    async def test_aclose_cancels_the_producer_and_releases_the_slot(self):
        d, conn = await dispatcher()
        reply = d.publish_streaming(b"", "REQUEST.X.Y.z", 60000)
        sid = stream_id(d)
        await tick(2)  # the request has gone out
        await reply.aclose()
        await tick(2)
        assert sid not in d.pending_streams
        cancels = [p for p in conn.publishes if p["exchange"] == Config.cancel_exchange_name()]
        assert len(cancels) == 1

    async def test_closing_before_the_request_went_out_withdraws_it(self):
        # Closed while parked on readiness: the request is never sent, and no
        # cancel notice is sent for a request the server never saw.
        conn = FakeConnection()
        conn.is_connected, conn.is_reconnecting = False, True
        ready = asyncio.Event()

        async def when_ready(timeout_ms=None):
            await ready.wait()

        conn.when_ready = when_ready
        d = MessageDispatcher(conn)
        d._channel = FakeChannel()
        d._is_initialized = True
        for close in (lambda r: r.aclose(), lambda r: r._on_abort() or _done()):
            reply = d.publish_streaming(b"req", "REQUEST.X.Y.z", 60000)
            sid = stream_id(d)
            await tick(2)
            await close(reply)
            ready.set()
            await tick(3)
            ready.clear()
            assert conn.publishes == []
            assert sid not in d.pending_streams
            with pytest.raises(StopAsyncIteration):
                await reply.__anext__()

    async def test_a_cancel_notice_never_overtakes_its_request(self):
        # Closed mid-send: the notice waits for the send to settle.
        conn = FakeConnection()
        gate = asyncio.Event()

        async def slow_publish(*_a, **_k):
            await gate.wait()

        conn.publish_hook = slow_publish
        d, _ = await dispatcher(conn)
        reply = d.publish_streaming(b"req", "REQUEST.X.Y.z", 60000)
        await tick(2)
        await reply.aclose()
        await tick(2)
        assert [p["exchange"] for p in conn.publishes] == [Config.bus_exchange_name()]
        conn.publish_hook = None
        gate.set()
        await tick(3)
        assert [p["exchange"] for p in conn.publishes] == [Config.bus_exchange_name(), Config.cancel_exchange_name()]

    async def test_cancelling_the_consumer_while_the_publish_is_pending_cleans_up(self):
        conn = FakeConnection()
        gate = asyncio.Event()

        async def slow_publish(*_a, **_k):
            await gate.wait()

        conn.publish_hook = slow_publish
        d, _ = await dispatcher(conn)
        reply = d.publish_streaming(b"req", "REQUEST.X.Y.z", 60000)
        sid = stream_id(d)
        consumer = asyncio.ensure_future(reply.__anext__())
        await tick(2)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert sid not in d.pending_streams
        conn.publish_hook = None
        gate.set()
        await tick(3)
        assert [p["exchange"] for p in conn.publishes][-1] == Config.cancel_exchange_name()

    async def test_a_cancelled_consumer_task_closes_the_stream(self):
        # asyncio cancellation of the task parked on the next chunk: nobody
        # will read any further, so the producer is told to stop, as it is
        # for aclose().
        d, conn = await dispatcher()
        reply = d.publish_streaming(b"", "REQUEST.X.Y.z", 60000)
        sid = stream_id(d)

        async def consume():
            async for _chunk in reply:
                pass

        task = asyncio.ensure_future(consume())
        await tick(3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await tick(2)
        assert sid not in d.pending_streams
        cancels = [p for p in conn.publishes if p["exchange"] == Config.cancel_exchange_name()]
        assert len(cancels) == 1
        assert cancels[0]["properties"]["correlation_id"] == sid

    async def test_async_with_closes_the_stream(self):
        d, conn = await dispatcher()
        async with d.publish_streaming(b"", "REQUEST.X.Y.z", 60000) as reply:
            sid = stream_id(d)
            await tick(2)
            body, headers = chunk(0, "a")
            await d._on_result(body, sid, headers)
            assert await reply.__anext__() == b"a"
        assert len(d.pending_streams) == 0

    async def test_a_publish_failure_surfaces_on_first_iteration(self):
        conn = FakeConnection()

        async def hook(*_a):
            raise RuntimeError("no route")

        conn.publish_hook = hook
        d, _ = await dispatcher(conn)
        reply = d.publish_streaming(b"", "REQUEST.X.Y.z", 1000)
        with pytest.raises(RuntimeError, match="no route"):
            await reply.__anext__()
        assert len(d.pending_streams) == 0

    async def test_a_disconnect_raises_rather_than_ending_the_stream_short(self):
        d, conn = await dispatcher()
        reply = d.publish_streaming(b"", "REQUEST.X.Y.z", 5000)
        sid = stream_id(d)
        body, headers = chunk(0, "a")
        await d._on_result(body, sid, headers)
        assert await reply.__anext__() == b"a"
        conn.emit("disconnected")
        with pytest.raises(DisconnectedError):
            await reply.__anext__()

    async def test_the_idle_timer_is_re_armed_on_progress(self):
        d, _ = await dispatcher()
        reply = d.publish_streaming(b"", "REQUEST.X.Y.z", 60)
        sid = stream_id(d)
        for i in range(4):
            await asyncio.sleep(0.03)
            body, headers = chunk(i, str(i))
            await d._on_result(body, sid, headers)
            assert await reply.__anext__() == str(i).encode()
        # 120ms elapsed with a 60ms idle window: progress kept it alive.


class TestStreamingSequenceValidation:
    async def test_accepts_chunks_arriving_in_order(self):
        d, _ = await dispatcher()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        for i, text in enumerate("abc"):
            body, headers = chunk(i, text)
            await d._on_result(body, sid, headers)
            assert await reply.__anext__() == text.encode()

    async def test_fails_the_stream_when_a_chunk_is_missing(self):
        d, _ = await dispatcher()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        body, headers = chunk(0, "a")
        await d._on_result(body, sid, headers)
        assert await reply.__anext__() == b"a"
        body, headers = chunk(2, "c")  # seq 1 never arrives
        await d._on_result(body, sid, headers)
        with pytest.raises(StreamSequenceError):
            await reply.__anext__()
        assert len(d.pending_streams) == 0

    async def test_drops_a_duplicate_chunk_instead_of_yielding_it_twice(self):
        d, _ = await dispatcher()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        body, headers = chunk(0, "a")
        await d._on_result(body, sid, headers)
        await d._on_result(body, sid, headers)  # redelivery
        assert await reply.__anext__() == b"a"
        body, headers = chunk(1, "b", True)
        await d._on_result(body, sid, headers)
        assert await reply.__anext__() == b"b"
        with pytest.raises(StopAsyncIteration):
            await reply.__anext__()

    async def test_stays_compatible_with_a_server_that_sends_no_sequence_header(self):
        d, _ = await dispatcher()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        await d._on_result(b"a", sid, {})
        await d._on_result(b"b", sid, {})
        assert await reply.__anext__() == b"a"
        assert await reply.__anext__() == b"b"

    async def test_tolerates_header_encodings(self):
        d, _ = await dispatcher()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        await d._on_result(b"a", sid, {Config.HEADER_SEQ: b"0", Config.HEADER_FINAL: "false"})
        await d._on_result(b"b", sid, {Config.HEADER_SEQ: "1", Config.HEADER_FINAL: "true"})
        assert await reply.__anext__() == b"a"
        assert await reply.__anext__() == b"b"
        with pytest.raises(StopAsyncIteration):
            await reply.__anext__()


class TestStreamingBackpressure:
    async def test_fails_the_stream_past_the_chunk_bound(self, monkeypatch):
        monkeypatch.setenv("STREAM_MAX_BUFFERED_CHUNKS", "3")
        d, _ = await dispatcher()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        for i in range(4):
            body, headers = chunk(i, f"c{i}")
            await d._on_result(body, sid, headers)
        with pytest.raises(StreamBackpressureError):
            await reply.__anext__()
        assert d._total_buffered_bytes == 0

    async def test_a_buffer_failure_stops_the_producer_without_waiting_for_a_pull(self, monkeypatch):
        # The consumer is paused. The dispatcher already knows the stream is
        # unusable, so the producer is told to stop NOW, the slot and the
        # buffer are released, later frames are ignored, and the error is
        # still what the next pull raises.
        monkeypatch.setenv("STREAM_MAX_BUFFERED_CHUNKS", "1")
        d, conn = await dispatcher()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        await tick(2)
        for i in range(2):
            body, headers = chunk(i, f"c{i}")
            await d._on_result(body, sid, headers)
        await tick(2)
        assert sid not in d.pending_streams
        assert d._total_buffered_bytes == 0
        cancels = [p for p in conn.publishes if p["exchange"] == Config.cancel_exchange_name()]
        assert len(cancels) == 1 and cancels[0]["properties"]["correlation_id"] == sid
        # A late frame for the terminated call is ignored.
        body, headers = chunk(2, "late")
        await d._on_result(body, sid, headers)
        assert d._total_buffered_bytes == 0
        with pytest.raises(StreamBackpressureError):
            await reply.__anext__()

    async def test_a_sequence_gap_stops_the_producer_without_waiting_for_a_pull(self):
        d, conn = await dispatcher()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        await tick(2)
        body, headers = chunk(0, "c0")
        await d._on_result(body, sid, headers)
        body, headers = chunk(2, "c2")
        await d._on_result(body, sid, headers)
        await tick(2)
        assert sid not in d.pending_streams
        assert len([p for p in conn.publishes if p["exchange"] == Config.cancel_exchange_name()]) == 1
        with pytest.raises(StreamSequenceError):
            await reply.__anext__()

    async def test_a_publish_failure_releases_the_slot_without_a_pull(self):
        conn = FakeConnection()

        async def hook(*_a):
            raise RuntimeError("no route")

        conn.publish_hook = hook
        d, _ = await dispatcher(conn)
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        await tick(3)
        assert sid not in d.pending_streams
        with pytest.raises(RuntimeError, match="no route"):
            await reply.__anext__()

    async def test_fails_the_stream_past_the_byte_bound(self, monkeypatch):
        monkeypatch.setenv("STREAM_MAX_BUFFERED_BYTES", "10")
        d, _ = await dispatcher()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        await d._on_result(b"x" * 8, sid, {Config.HEADER_SEQ: 0})
        await d._on_result(b"x" * 8, sid, {Config.HEADER_SEQ: 1})
        with pytest.raises(StreamBackpressureError):
            await reply.__anext__()

    async def test_the_aggregate_bound_spans_every_stream(self, monkeypatch):
        monkeypatch.setenv("STREAM_MAX_TOTAL_BUFFERED_BYTES", "10")
        d, _ = await dispatcher()
        a = d.publish_streaming(b"req", "R.A.B.c", 5000)
        b = d.publish_streaming(b"req", "R.A.B.c", 5000)
        ids = list(d.pending_streams)
        await d._on_result(b"x" * 6, ids[0], {})
        await d._on_result(b"x" * 6, ids[1], {})
        assert await a.__anext__() == b"x" * 6
        with pytest.raises(StreamBackpressureError):
            await b.__anext__()

    async def test_consumed_bytes_are_returned_to_the_allowance(self):
        d, _ = await dispatcher()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        await d._on_result(b"abc", sid, {})
        assert d._total_buffered_bytes == 3
        await reply.__anext__()
        assert d._total_buffered_bytes == 0


def stream_request(correlation_id):
    return make_delivery(body=b"req", routing_key="REQUEST.S.A.stream", correlation_id=correlation_id, reply_to="callback.q")


class TestServerSideStreamCancellation:
    async def test_a_producer_that_raises_on_cancellation_is_settled_not_left_unacked(self):
        # signal.throw_if_aborted() raises CancelledError inside the
        # generator. That is cancelled work, to be acknowledged — not the
        # consumer being torn down — or the delivery would occupy the
        # worker's only prefetch slot until the channel closed.
        conn = Connection()
        ch = FakeChannel()
        entered = asyncio.Event()

        async def handler(_c, _id, _h, context):
            async def gen():
                entered.set()
                await context.signal.wait()
                context.signal.throw_if_aborted()
                yield b"unreachable"

            return gen()

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        delivery = asyncio.ensure_future(ch.deliver(stream_request("cid-throw")))
        await entered.wait()
        assert conn.cancel_stream("cid-throw") is True
        await asyncio.wait_for(delivery, 1)
        assert not delivery.cancelled()
        assert len(ch.acked) == 1 and ch.rejected == []
        assert conn.in_flight_deliveries == 0
        # Nothing was published for the cancelled stream.
        assert [p for p in ch.published if p["exchange"] == Config.callbacks_exchange_name()] == []

        # The worker is free: a second delivery is served normally.
        async def plain(_c, _id, _h, context):
            async def gen():
                yield b"a"

            return gen()

        ch2 = FakeChannel()
        await conn.consume(ch2, "Q", plain, ConsumeOptions(), True)
        await ch2.deliver(stream_request("cid-next"))
        assert len(ch2.acked) == 1

    async def test_a_unary_handler_that_raises_on_cancellation_is_settled_too(self):
        conn = Connection()
        ch = FakeChannel()
        entered = asyncio.Event()

        async def handler(_c, _id, _h, context):
            entered.set()
            await context.signal.wait()
            context.signal.throw_if_aborted()
            return b"unreachable"

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True, retry_options=ConsumeRetryOptions(max_retries=3, retry_queue_name="Q.Retry", dlq_name="Q.DLQ", retry_exchange_name="Q.Retry.Exchange"))
        delivery = asyncio.ensure_future(ch.deliver(stream_request("cid-unary")))
        await entered.wait()
        conn.cancel_stream("cid-unary")
        await asyncio.wait_for(delivery, 1)
        # Acked, and NOT sent round the retry ladder.
        assert len(ch.acked) == 1 and ch.rejected == []
        assert [p for p in ch.published if p["exchange"] == "Q.Retry.Exchange"] == []

    async def test_cancelling_the_consumer_task_itself_still_propagates(self):
        # Teardown: nobody cancelled the delivery, the task hosting it is
        # being cancelled. That must not be mistaken for cancelled work and
        # acknowledged.
        conn = Connection()
        ch = FakeChannel()
        entered = asyncio.Event()

        async def handler(_c, _id, _h, context):
            async def gen():
                entered.set()
                await asyncio.Event().wait()
                yield b"unreachable"

            return gen()

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        delivery = asyncio.ensure_future(ch.deliver(stream_request("cid-teardown")))
        await entered.wait()
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery
        assert ch.acked == [] and ch.rejected == []

    async def test_aborts_the_handler_signal_when_the_stream_is_cancelled(self):
        conn = Connection()
        ch = FakeChannel()
        seen = {}

        async def handler(_c, _id, _h, context):
            seen["signal"] = context.signal
            context.signal.add_listener(lambda: seen.__setitem__("aborted", True))

            async def gen():
                for i in range(1000):
                    if context.signal.aborted:
                        return
                    yield f"chunk-{i}".encode()
                    await asyncio.sleep(0.001)

            return gen()

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        delivery = asyncio.ensure_future(ch.deliver(stream_request("cid-cancel")))
        await asyncio.sleep(0.02)
        assert "signal" in seen
        assert conn.cancel_stream("cid-cancel") is True
        await delivery
        assert seen.get("aborted") is True
        assert len(ch.published) < 100

    async def test_stops_publishing_chunks_once_cancelled(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            async def gen():  # ignores its signal
                for i in range(50):
                    yield f"chunk-{i}".encode()
                    await asyncio.sleep(0.001)

            return gen()

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        delivery = asyncio.ensure_future(ch.deliver(stream_request("cid-uncoop")))
        await asyncio.sleep(0.01)
        conn.cancel_stream("cid-uncoop")
        await delivery
        assert len(ch.published) < 50

    async def test_acknowledges_a_cancelled_request_instead_of_retrying_it(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(_c, _id, _h, context):
            async def gen():
                while not context.signal.aborted:
                    yield b"x"
                    await asyncio.sleep(0.001)

            return gen()

        retry = ConsumeRetryOptions(max_retries=3, retry_queue_name="Q.Retry", retry_exchange_name="Q.Retry.Ex", dlq_name="Q.DLQ")
        await conn.consume(ch, "Q", handler, ConsumeOptions(), True, retry)
        delivery = asyncio.ensure_future(ch.deliver(stream_request("cid-ack")))
        await asyncio.sleep(0.01)
        conn.cancel_stream("cid-ack")
        await delivery
        assert len(ch.acked) == 1
        assert not any(p.exchange == "Q.Retry.Ex" for p in ch.published)

    async def test_reports_false_when_cancelling_an_unknown_stream(self):
        assert Connection().cancel_stream("never-heard-of-it") is False

    async def test_releases_its_bookkeeping_when_the_stream_finishes_normally(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            async def gen():
                yield b"only"

            return gen()

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        await ch.deliver(stream_request("cid-done"))
        assert conn.cancel_stream("cid-done") is False

    async def test_publishes_chunks_with_sequence_and_final_headers(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            async def gen():
                yield b"a"
                yield b"b"

            return gen()

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        await ch.deliver(stream_request("cid-seq"))
        replies = ch.published_to(Config.callbacks_exchange_name())
        assert [(r.content, r.headers[Config.HEADER_SEQ], r.headers[Config.HEADER_FINAL]) for r in replies] == [
            (b"a", 0, False), (b"b", 1, True),
        ]
        assert all(r.properties.correlation_id == "cid-seq" for r in replies)
        assert len(ch.acked) == 1

    async def test_an_empty_stream_publishes_a_single_terminal_marker(self):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            async def gen():
                return
                yield  # pragma: no cover

            return gen()

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        await ch.deliver(stream_request("cid-empty"))
        replies = ch.published_to(Config.callbacks_exchange_name())
        assert [(r.content, r.headers[Config.HEADER_FINAL]) for r in replies] == [(b"", True)]

    async def test_the_generator_is_closed_when_the_stream_is_cancelled(self):
        conn = Connection()
        ch = FakeChannel()
        closed = asyncio.Event()

        async def handler(*_a):
            async def gen():
                try:
                    for i in range(1000):
                        yield b"x"
                        await asyncio.sleep(0.001)
                finally:
                    closed.set()

            return gen()

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        delivery = asyncio.ensure_future(ch.deliver(stream_request("cid-close")))
        await asyncio.sleep(0.01)
        conn.cancel_stream("cid-close")
        await delivery
        assert closed.is_set()


class TestTheEncodingWrapperOwnsTheHandlerIterator:
    """Closing what the connection holds — the encoding wrapper — must close
    the handler's own async generator, or the upstream resource it holds
    (an HTTP stream to a model provider) outlives the call."""

    def service(self):
        from protobus import Context, MessageService
        from protobus.message_factory import MessageFactory

        factory = MessageFactory()
        factory.init()
        factory.parse('syntax = "proto3"; package Res; message M { string s = 1; } service S { rpc stream(Res.M) returns (stream Res.M); }', "Res.S")

        class Service(MessageService):
            service_name = "Res.S"
            proto_file_name = ""

        ctx = Context(FakeConnection())
        ctx._message_factory = factory
        return Service(ctx)

    async def test_closing_the_wrapper_closes_the_upstream_generator(self):
        svc = self.service()
        closed = []

        async def upstream():
            try:
                yield {"s": "one"}
                yield {"s": "two"}
            finally:
                closed.append(True)

        inner = upstream()
        wrapper = svc._stream_responses("Res.S.stream", inner)
        await wrapper.__anext__()
        assert closed == []
        await wrapper.aclose()
        assert closed == [True]

    async def test_an_upstream_context_manager_is_left_before_the_wrapper_finishes_closing(self):
        svc = self.service()
        events = []

        class Upstream:
            async def __aenter__(self):
                events.append("enter")
                return self

            async def __aexit__(self, *_exc):
                events.append("exit")

        async def upstream():
            async with Upstream():
                yield {"s": "one"}
                yield {"s": "two"}

        wrapper = svc._stream_responses("Res.S.stream", upstream())
        await wrapper.__anext__()
        await wrapper.aclose()
        events.append("wrapper closed")
        assert events == ["enter", "exit", "wrapper closed"]

    async def test_a_handler_error_still_closes_the_generator_and_yields_the_error(self):
        svc = self.service()
        closed = []

        async def upstream():
            try:
                yield {"s": "one"}
                raise RuntimeError("upstream broke")
            finally:
                closed.append(True)

        wrapper = svc._stream_responses("Res.S.stream", upstream())
        out = [c async for c in wrapper]
        assert len(out) == 2  # the chunk, then the terminal error
        assert closed == [True]


class TestACompletedStreamIsCollectable:
    """Nothing the dispatcher holds — the pending entry, the finalizer — may
    lead back to a StreamingReply the application has dropped. Otherwise
    every completed call accumulates for the life of the process."""

    async def _run(self, conn, scenario):
        import weakref

        d = MessageDispatcher(conn)
        await d.init()
        ac = AbortController()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000 if scenario != "timeout" else 10, StreamOptions(signal=ac.signal))
        sid = stream_id(d)
        await tick(2)
        if scenario == "consumed":
            body, headers = chunk(0, "a", final=True)
            await d._on_result(body, sid, headers)
            assert [c async for c in reply] == [b"a"]
        elif scenario == "closed":
            await reply.aclose()
        elif scenario == "failed":
            body, headers = chunk(2, "gap")
            await d._on_result(body, sid, headers)
            with pytest.raises(StreamSequenceError):
                await reply.__anext__()
        elif scenario == "timeout":
            with pytest.raises(StreamTimeoutError):
                await reply.__anext__()
        elif scenario == "abandoned":
            pass  # dropped without ever iterating or closing
        elif scenario == "aborted":
            ac.abort()
            with pytest.raises(StopAsyncIteration):
                await reply.__anext__()
        ref = weakref.ref(reply)
        del reply
        await tick(3)
        import gc

        gc.collect()
        await tick(2)
        return ref, d

    @pytest.mark.parametrize("scenario", ["consumed", "closed", "failed", "timeout", "aborted"])
    async def test_every_terminal_path_releases_the_object(self, scenario):
        conn = FakeConnection()
        ref, d = await self._run(conn, scenario)
        assert ref() is None, f"a {scenario} stream is still alive after collection"
        assert len(d.pending_streams) == 0 and d._total_buffered_bytes == 0

    async def test_a_dropped_stream_is_released_once_its_idle_deadline_passes(self):
        import gc
        import weakref

        conn = FakeConnection()
        d = MessageDispatcher(conn)
        await d.init()
        reply = d.publish_streaming(b"req", "R.A.B.c", 20)
        sid = stream_id(d)
        ref = weakref.ref(reply)
        del reply
        await asyncio.sleep(0.05)  # the idle timer, the last strong holder, fires
        gc.collect()
        await tick(2)
        assert ref() is None
        assert sid not in d.pending_streams

    async def test_the_finalizer_alone_releases_a_dropped_entry_without_a_timer(self):
        # Belt and braces: with the idle timer out of the picture, dropping
        # the reply still releases the entry through the finalizer.
        import gc

        conn = FakeConnection()
        d = MessageDispatcher(conn)
        await d.init()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        sid = stream_id(d)
        await tick(2)
        reply._clear_idle()
        del reply
        gc.collect()
        await tick(2)
        assert sid not in d.pending_streams


class TestATerminalOutcomeUnblocksACallerWaitingOnThePublish:
    """A send stalled on the confirm must not hold the caller past the
    stream's own idle deadline, close or abort. The send stays owned."""

    async def stalled(self):
        conn = FakeConnection()
        gate = asyncio.Event()

        async def hold(*_a, **_k):
            await gate.wait()

        conn.publish_hook = hold
        d, _ = await dispatcher(conn)
        return conn, d, gate

    async def test_the_idle_deadline_raises_promptly(self):
        conn, d, gate = await self.stalled()
        reply = d.publish_streaming(b"req", "R.A.B.c", 20)
        pull = asyncio.ensure_future(reply.__anext__())
        await asyncio.sleep(0.06)
        assert pull.done()
        with pytest.raises(StreamTimeoutError):
            await pull
        assert len(d.pending_streams) == 0
        # The send settles later without disturbing the outcome, and the
        # notice follows the request rather than preceding it.
        gate.set()
        conn.publish_hook = None
        await tick(3)
        assert [p["exchange"] for p in conn.publishes] == [Config.bus_exchange_name(), Config.cancel_exchange_name()]
        with pytest.raises(StreamTimeoutError):
            await reply.__anext__()

    async def test_close_ends_the_wait(self):
        conn, d, gate = await self.stalled()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        pull = asyncio.ensure_future(reply.__anext__())
        await tick(2)
        await reply.aclose()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pull, 0.5)
        gate.set()
        conn.publish_hook = None
        await tick(3)
        assert [p["exchange"] for p in conn.publishes] == [Config.bus_exchange_name(), Config.cancel_exchange_name()]

    async def test_abort_ends_the_wait(self):
        conn, d, gate = await self.stalled()
        ac = AbortController()
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000, StreamOptions(signal=ac.signal))
        pull = asyncio.ensure_future(reply.__anext__())
        await tick(2)
        ac.abort()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pull, 0.5)
        gate.set()
        await tick(3)

    async def test_a_late_publish_failure_does_not_overwrite_the_outcome(self):
        conn, d, gate = await self.stalled()
        reply = d.publish_streaming(b"req", "R.A.B.c", 20)
        with pytest.raises(StreamTimeoutError):
            await asyncio.wait_for(reply.__anext__(), 0.5)

        async def fail(*_a, **_k):
            raise RuntimeError("late nack")

        conn.publish_hook = fail
        gate.set()
        await tick(3)
        with pytest.raises(StreamTimeoutError):
            await reply.__anext__()


class TestTheCancelNoticeFollowsTheDeliveryOutcome:
    """Only a request the server definitely never saw goes without a notice.
    An ambiguous send — a confirm timeout, a closed channel — may have
    reached a producer, which must be told."""

    async def failing(self, error):
        conn = FakeConnection()

        async def fail(*_a, **_k):
            raise error

        conn.publish_hook = fail
        d, _ = await dispatcher(conn)
        reply = d.publish_streaming(b"req", "R.A.B.c", 5000)
        with pytest.raises(type(error)):
            await reply.__anext__()
        await reply.aclose()
        await tick(3)
        return [p for p in conn.publishes if p["exchange"] == Config.cancel_exchange_name()]

    async def test_a_confirm_timeout_sends_a_notice(self):
        from protobus import PublishConfirmTimeoutError

        assert len(await self.failing(PublishConfirmTimeoutError("no confirm", "m"))) == 1

    async def test_a_closed_channel_sends_a_notice(self):
        from protobus import ChannelClosedError

        assert len(await self.failing(ChannelClosedError("closed", "m"))) == 1

    async def test_an_unexpected_error_is_treated_as_ambiguous(self):
        assert len(await self.failing(RuntimeError("what happened?"))) == 1

    async def test_a_nack_sends_none(self):
        from protobus import PublishNackedError

        assert await self.failing(PublishNackedError("nacked", "m")) == []

    async def test_an_unroutable_return_sends_none(self):
        from protobus import UnroutableError

        assert await self.failing(UnroutableError("no route", "m")) == []
