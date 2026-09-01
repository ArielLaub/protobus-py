"""
Integration tests for server-streaming RPC against a real RabbitMQ broker.

These tests verify the wire-protocol guarantees documented in
``docs/advanced/streaming.md``:

- Multi-chunk delivery in order
- ``x-protobus-final`` header correctly terminates the client iterator
- Mid-stream errors are raised inside ``async for``
- Empty-generator streams end cleanly without yielding spurious chunks
- Early ``break`` releases the pending-stream slot in the dispatcher
- Unary RPCs declared in the same .proto are unaffected (backward compat)

Run with a live broker:

    docker-compose up -d
    pytest tests/test_streaming.py -v
"""

import asyncio
import os
from typing import AsyncIterator

import pytest

from protobus import Context, MessageService, ServiceProxy
from protobus.errors import HandledError


from .broker_url import broker_url

RABBITMQ_URL = broker_url()
PROTO_DIR = os.path.join(os.path.dirname(__file__), "streaming_proto")


class CounterService(MessageService):
    """
    Exercises every streaming code path the framework supports.

    See ``tests/streaming_proto/streaming_test.proto`` for the contract.
    """

    @property
    def service_name(self) -> str:
        return "streaming_test.Counter"

    @property
    def proto_file_name(self) -> str:
        return "streaming_test.proto"

    @property
    def Proto(self) -> str:
        with open(os.path.join(PROTO_DIR, "streaming_test.proto")) as f:
            return f.read()

    # Unary — confirms streaming changes haven't broken existing methods.
    async def add(self, data: dict, actor: str, correlation_id: str) -> dict:
        return {"sum": data.get("a", 0) + data.get("b", 0)}

    # Streaming — an `async def` with `yield` becomes an async-generator
    # function, and the framework auto-detects it via inspect.isasyncgenfunction.
    async def tick(
        self,
        data: dict,
        actor: str,
        correlation_id: str,
    ) -> AsyncIterator[dict]:
        count = int(data.get("count", 0))
        fail_at = int(data.get("fail_at", 0))
        emit_nothing = bool(data.get("emit_nothing", False))

        if emit_nothing:
            return

        for i in range(count):
            if fail_at and i >= fail_at:
                raise HandledError(
                    f"deliberate failure at chunk {i}", code="TEST_FAIL"
                )
            yield {"seq": i, "payload": f"chunk-{i}"}


@pytest.fixture
async def context():
    """A Context with the streaming-test proto loaded so server_streaming flags resolve."""
    ctx = Context()
    await ctx.init(RABBITMQ_URL, proto_dirs=[PROTO_DIR])
    yield ctx
    await ctx.close()


@pytest.fixture
async def counter_service(context):
    service = CounterService(context)
    await service.init()
    yield service


@pytest.fixture
async def counter_proxy(context, counter_service):
    proxy = ServiceProxy(context, "streaming_test.Counter")
    await proxy.init()
    yield proxy


class TestStreamingFlagDetection:
    """The framework must read the gRPC `stream` keyword from the descriptor pool."""

    async def test_streaming_method_is_detected(self, context, counter_service):
        # Reading the flag directly off the factory is the only place the
        # framework cares whether a method is streaming.
        assert context.factory.is_streaming_method("streaming_test.Counter.tick") is True

    async def test_unary_method_is_not_streaming(self, context, counter_service):
        assert context.factory.is_streaming_method("streaming_test.Counter.add") is False

    async def test_unknown_method_is_not_streaming(self, context):
        # No KeyError leaks out — a missing method just returns False.
        assert context.factory.is_streaming_method("streaming_test.Counter.nope") is False


class TestStreamingBackwardCompat:
    """Adding streaming must not change unary call behavior."""

    async def test_unary_add_still_works(self, counter_proxy):
        result = await counter_proxy.add({"a": 5, "b": 7})
        assert result["sum"] == 12


class TestStreamingHappyPath:
    """Basic multi-chunk delivery, ordering, and clean termination."""

    async def test_five_chunks_arrive_in_order(self, counter_proxy):
        chunks = []
        async for chunk in counter_proxy.tick({"count": 5}):
            chunks.append(chunk)

        assert len(chunks) == 5
        # Verify ordering via `payload` (proto3 strips scalar defaults — seq=0
        # would be absent from the first chunk's dict). The string payload
        # field is always non-default so it's the right ordering signal.
        for i, chunk in enumerate(chunks):
            assert chunk["payload"] == f"chunk-{i}"

    async def test_single_chunk_stream(self, counter_proxy):
        chunks = []
        async for chunk in counter_proxy.tick({"count": 1}):
            chunks.append(chunk)
        assert len(chunks) == 1

    async def test_empty_stream_ends_cleanly(self, counter_proxy):
        """A generator that yields nothing must produce zero chunks, not raise."""
        chunks = []
        async for chunk in counter_proxy.tick({"emit_nothing": True}):
            chunks.append(chunk)
        assert chunks == []


class TestStreamingErrors:
    """Mid-stream errors must surface inside the iteration."""

    async def test_error_mid_stream_raises(self, counter_proxy):
        """Server raises after 2 chunks → client gets 2 chunks then exception."""
        chunks = []
        with pytest.raises(Exception) as exc_info:
            async for chunk in counter_proxy.tick({"count": 10, "fail_at": 2}):
                chunks.append(chunk)

        assert len(chunks) == 2
        assert chunks[0]["payload"] == "chunk-0"
        assert chunks[1]["payload"] == "chunk-1"
        assert "deliberate failure" in str(exc_info.value)
        # HandledError attaches a code; the proxy preserves it on the raised Exception.
        assert getattr(exc_info.value, "code", None) == "TEST_FAIL"

    async def test_error_on_first_chunk_raises(self, counter_proxy):
        """fail_at=1 → server emits chunk 0, then raises before emitting chunk 1."""
        chunks = []
        with pytest.raises(Exception):
            async for chunk in counter_proxy.tick({"count": 10, "fail_at": 1}):
                chunks.append(chunk)
        assert len(chunks) == 1
        assert chunks[0]["payload"] == "chunk-0"


class TestStreamingEarlyTermination:
    """Breaking out of the iterator must clean up dispatcher state."""

    async def test_break_releases_pending_slot(self, counter_proxy, context):
        n = 0
        async for chunk in counter_proxy.tick({"count": 100}):
            n += 1
            if n >= 3:
                break

        # Give the framework a moment to drain
        await asyncio.sleep(0.1)

        # The pending-streams map is internal; we read it through the dispatcher.
        dispatcher = context._message_dispatcher
        assert len(dispatcher._pending_streams) == 0, (
            "early break should release the pending-stream correlation"
        )


class TestStreamingConcurrent:
    """Multiple in-flight streams from one proxy must not cross-contaminate."""

    async def test_two_streams_in_parallel(self, counter_proxy):
        async def collect(req):
            return [c async for c in counter_proxy.tick(req)]

        # Two streams from the same proxy at the same time. RabbitMQ
        # multiplexes them on the single callback queue by correlation_id;
        # the dispatcher routes each chunk to the right pending queue.
        a_task = asyncio.create_task(collect({"count": 5}))
        b_task = asyncio.create_task(collect({"count": 8}))

        a, b = await asyncio.gather(a_task, b_task)

        assert len(a) == 5
        assert len(b) == 8
        # Sequences are independent — verify via payload (seq=0 strips by proto3).
        assert [c["payload"] for c in a] == [f"chunk-{i}" for i in range(5)]
        assert [c["payload"] for c in b] == [f"chunk-{i}" for i in range(8)]
