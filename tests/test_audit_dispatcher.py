"""
An RPC call and a streaming call must each have a deadline and a truthful end.

Three separate audit findings meet in MessageDispatcher:

1. A unary call's default deadline was ``MESSAGE_PROCESSING_TIMEOUT`` — the
   *server's* budget for running a handler, 600 000 ms. A caller to a service
   that is scaled to zero blocked for ten minutes. TS carries a separate
   ``RPC_CALL_TIMEOUT_MS`` for the caller side (908d5c8, and the distinction is
   spelled out again in 2fee268).

2. Losing the connection mid-stream pushed ``_STREAM_END`` into every pending
   stream, which is the same sentinel a *complete* stream ends with. The caller
   saw a truncated stream as a successful short one. The docstring claimed the
   iterator would raise ``StreamTimeoutError``; it does not.

3. The chunk queue was unbounded. ``StreamBackpressureError`` has existed in
   errors.py since streaming shipped without ever being raised (310da84 bounds
   buffered chunks and bytes on the TS side).
"""

import asyncio

import pytest

from protobus.config import Config
from protobus.errors import (
    DisconnectedError,
    RpcTimeoutError,
    StreamBackpressureError,
)
from protobus.message_dispatcher import MessageDispatcher


class _FakeConnection:
    def __init__(self):
        self.handlers = {}
        self.is_connected = True

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def off(self, event, callback):
        self.handlers.get(event, []).remove(callback)


class _FakeExchange:
    def __init__(self):
        self.published = []

    async def publish(self, message, routing_key, **kwargs):
        self.published.append((message, routing_key, kwargs))


class _FakeCallbackListener:
    callback_queue = "amq.gen-fake-callback-queue"


def _dispatcher():
    """A dispatcher wired to fakes, past init()."""
    d = MessageDispatcher(_FakeConnection())
    d._is_initialized = True
    d._channel = object()
    d._exchange = _FakeExchange()
    d._callback_listener = _FakeCallbackListener()
    return d


class TestTheCallerHasItsOwnDeadline:
    def test_the_default_rpc_deadline_is_not_the_server_processing_budget(self):
        assert Config.rpc_call_timeout() != Config.message_processing_timeout()
        assert Config.rpc_call_timeout() == 30000

    def test_the_rpc_deadline_is_configurable(self, monkeypatch):
        monkeypatch.setenv("RPC_CALL_TIMEOUT_MS", "1234")
        assert Config.rpc_call_timeout() == 1234

    async def test_an_unanswered_call_raises_rpc_timeout(self, monkeypatch):
        monkeypatch.setenv("RPC_CALL_TIMEOUT_MS", "150")
        d = _dispatcher()
        with pytest.raises(RpcTimeoutError):
            await asyncio.wait_for(
                d.publish(b"body", "REQUEST.Nobody.listening", rpc=True), timeout=5
            )

    async def test_a_timed_out_call_leaves_no_pending_entry(self, monkeypatch):
        monkeypatch.setenv("RPC_CALL_TIMEOUT_MS", "150")
        d = _dispatcher()
        with pytest.raises(RpcTimeoutError):
            await d.publish(b"body", "REQUEST.Nobody.listening", rpc=True)
        assert d._pending_callbacks == {}

    async def test_an_explicit_timeout_argument_still_wins(self, monkeypatch):
        monkeypatch.setenv("RPC_CALL_TIMEOUT_MS", "60000")
        d = _dispatcher()
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(RpcTimeoutError):
            await d.publish(b"body", "REQUEST.X.y", rpc=True, timeout_ms=150)
        assert loop.time() - started < 2


class TestATruncatedStreamIsNotAFinishedStream:
    async def test_disconnect_mid_stream_raises_rather_than_ending_cleanly(self):
        d = _dispatcher()

        chunks = []

        async def consume():
            async for chunk in d.publish_streaming(b"body", "REQUEST.X.stream"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        # Let publish_streaming register its pending-stream slot.
        for _ in range(20):
            await asyncio.sleep(0)
            if d._pending_streams:
                break
        assert d._pending_streams, "stream slot was never registered"

        correlation_id = next(iter(d._pending_streams))
        await d._on_result(b"chunk-1", correlation_id, {})
        await asyncio.sleep(0)

        d._on_disconnected()

        with pytest.raises(DisconnectedError):
            await asyncio.wait_for(task, timeout=5)

        assert chunks == [b"chunk-1"], "the delivered prefix should still be seen"

    async def test_a_complete_stream_still_ends_cleanly(self):
        d = _dispatcher()
        chunks = []

        async def consume():
            async for chunk in d.publish_streaming(b"body", "REQUEST.X.stream"):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        for _ in range(20):
            await asyncio.sleep(0)
            if d._pending_streams:
                break
        correlation_id = next(iter(d._pending_streams))

        await d._on_result(b"chunk-1", correlation_id, {Config.HEADER_SEQ: 0})
        await d._on_result(
            b"chunk-2", correlation_id, {Config.HEADER_SEQ: 1, Config.HEADER_FINAL: True}
        )
        await asyncio.wait_for(task, timeout=5)
        assert chunks == [b"chunk-1", b"chunk-2"]


class TestTheStreamBufferIsBounded:
    def test_the_bound_is_configurable_and_has_a_default(self, monkeypatch):
        assert Config.stream_max_buffered_chunks() == 256
        monkeypatch.setenv("STREAM_MAX_BUFFERED_CHUNKS", "8")
        assert Config.stream_max_buffered_chunks() == 8

    async def test_a_producer_outrunning_the_consumer_raises(self, monkeypatch):
        monkeypatch.setenv("STREAM_MAX_BUFFERED_CHUNKS", "4")
        d = _dispatcher()
        gate = asyncio.Event()
        seen = []

        async def consume():
            # Stalls after the first chunk, then drains: the producer gets to
            # outrun it, and the drain is what surfaces the failure.
            async for chunk in d.publish_streaming(b"body", "REQUEST.X.stream"):
                seen.append(chunk)
                await gate.wait()

        task = asyncio.create_task(consume())
        for _ in range(20):
            await asyncio.sleep(0)
            if d._pending_streams:
                break
        correlation_id = next(iter(d._pending_streams))

        for i in range(50):
            await d._on_result(f"chunk-{i}".encode(), correlation_id, {})
            await asyncio.sleep(0)

        gate.set()
        with pytest.raises(StreamBackpressureError):
            await asyncio.wait_for(task, timeout=5)

        assert len(seen) <= 5, "the buffer was not actually bounded"
