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
        await reply.aclose()
        await tick(2)
        assert sid not in d.pending_streams
        cancels = [p for p in conn.publishes if p["exchange"] == Config.cancel_exchange_name()]
        assert len(cancels) == 1

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
