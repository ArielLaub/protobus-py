"""Server-streaming RPC against a real broker, including remote cancellation."""

import asyncio
import contextlib

import pytest

from protobus import AbortController, Context, HandledError, MessageService, RemoteError, RetryOptions, ServiceProxy, StreamOptions

from .conftest import unique

PKG = unique("streaming_test")
PROTO = f"""syntax = "proto3";
package {PKG};
message TickRequest {{ int32 count = 1; int32 fail_at = 2; bool emit_nothing = 3; int32 delay_ms = 4; }}
message Tick {{ int32 seq = 1; string payload = 2; }}
message AddRequest {{ int32 a = 1; int32 b = 2; }}
message AddResponse {{ int32 sum = 1; }}
service Counter {{
    rpc add (AddRequest) returns (AddResponse);
    rpc tick (TickRequest) returns (stream Tick);
}}"""


class CounterService(MessageService):
    service_name = f"{PKG}.Counter"
    proto_file_name = ""
    Proto = PROTO

    def __init__(self, context):
        super().__init__(context, retry=RetryOptions(max_retries=0))
        self.produced = 0
        self.saw_abort = False
        self.finished = False

    async def add(self, request, actor, correlation_id):
        return {"sum": request.get("a", 0) + request.get("b", 0)}

    async def tick(self, request, actor, correlation_id, context):
        count = request.get("count", 0)
        fail_at = request.get("fail_at", 0)
        if request.get("emit_nothing"):
            return
        self.finished = False
        try:
            for i in range(count):
                if fail_at and i >= fail_at:
                    raise HandledError(f"deliberate failure at chunk {i}", "TEST_FAIL")
                if context.signal.aborted:
                    self.saw_abort = True
                    return
                self.produced = i + 1
                yield {"seq": i, "payload": f"chunk-{i}"}
                if request.get("delay_ms"):
                    await asyncio.sleep(request["delay_ms"] / 1000)
        finally:
            self.finished = True


@pytest.fixture
async def stack(amqp_url, cleanup_queues):
    context = Context()
    await context.init(amqp_url, [])
    svc = CounterService(context)
    await svc.init()
    cleanup_queues.extend([f"{PKG}.Counter", f"{PKG}.Counter.Events"])
    client = ServiceProxy(context, svc.service_name)
    await client.init()
    yield context, svc, client
    await svc.close()
    await context.close()


class TestFlagDetection:
    async def test_detects_the_stream_keyword(self, stack):
        context, _, _ = stack
        assert context.factory.is_streaming_method(f"{PKG}.Counter.tick") is True
        assert context.factory.is_streaming_method(f"{PKG}.Counter.add") is False
        assert context.factory.is_streaming_method(f"{PKG}.Counter.nope") is False


class TestHappyPath:
    async def test_delivers_five_chunks_in_order(self, stack):
        _, _, client = stack
        chunks = [c async for c in client.tick({"count": 5})]
        assert [c["payload"] for c in chunks] == [f"chunk-{i}" for i in range(5)]
        assert [c["seq"] for c in chunks] == [0, 1, 2, 3, 4]

    async def test_handles_a_single_chunk_stream(self, stack):
        _, _, client = stack
        assert len([c async for c in client.tick({"count": 1})]) == 1

    async def test_handles_an_empty_stream_cleanly(self, stack):
        _, _, client = stack
        assert [c async for c in client.tick({"emit_nothing": True})] == []

    async def test_unary_add_still_works_alongside_streaming(self, stack):
        _, _, client = stack
        assert await client.add({"a": 5, "b": 7}) == {"sum": 12}


class TestErrors:
    async def test_raises_mid_stream_errors_inside_the_loop(self, stack):
        _, _, client = stack
        chunks = []
        with pytest.raises(RemoteError) as info:
            async for chunk in client.tick({"count": 10, "fail_at": 2}):
                chunks.append(chunk)
        assert [c["payload"] for c in chunks] == ["chunk-0", "chunk-1"]
        assert "deliberate failure" in info.value.message
        assert info.value.code == "TEST_FAIL"

    async def test_raises_after_only_one_chunk_if_fail_at_is_1(self, stack):
        _, _, client = stack
        chunks = []
        with pytest.raises(RemoteError):
            async for chunk in client.tick({"count": 10, "fail_at": 1}):
                chunks.append(chunk)
        assert len(chunks) == 1


class TestCancellation:
    async def test_stops_the_producer_when_the_caller_closes_the_stream(self, stack):
        _, svc, client = stack
        svc.produced = 0
        svc.saw_abort = False
        received = []
        async with client.tick({"count": 200, "delay_ms": 20}) as stream:
            async for tick in stream:
                received.append(tick)
                if len(received) == 3:
                    break
        assert len(received) == 3
        produced_at_break = svc.produced
        await asyncio.sleep(0.3)
        assert svc.saw_abort is True
        assert svc.produced < produced_at_break + 10
        assert svc.finished is True

    async def test_stops_the_producer_when_an_abort_signal_fires_outside_the_loop(self, stack):
        _, svc, client = stack
        svc.produced = 0
        svc.saw_abort = False
        stop = AbortController()
        received = []
        asyncio.get_running_loop().call_later(0.12, stop.abort)
        with contextlib.suppress(Exception):
            async for tick in client.tick({"count": 200, "delay_ms": 20}, None, 10000, StreamOptions(signal=stop.signal)):
                received.append(tick)
        assert len(received) > 0
        produced_at_stop = svc.produced
        await asyncio.sleep(0.3)
        assert svc.saw_abort is True
        assert svc.produced < produced_at_stop + 10

    async def test_leaves_an_uncancelled_stream_completely_unaffected(self, stack):
        _, _, client = stack
        ticks = [t async for t in client.tick({"count": 4})]
        assert [t["payload"] for t in ticks] == ["chunk-0", "chunk-1", "chunk-2", "chunk-3"]


class TestEarlyTermination:
    async def test_releases_pending_stream_state_on_close(self, stack):
        context, _, client = stack
        n = 0
        async with client.tick({"count": 100}) as stream:
            async for _chunk in stream:
                n += 1
                if n >= 3:
                    break
        await asyncio.sleep(0.05)
        assert len(context.message_dispatcher.pending_streams) == 0


class TestConcurrentStreams:
    async def test_runs_two_streams_on_the_same_proxy_without_cross_talk(self, stack):
        _, _, client = stack

        async def collect(req):
            return [c async for c in client.tick(req)]

        a, b = await asyncio.gather(collect({"count": 5}), collect({"count": 8}))
        assert [c["payload"] for c in a] == [f"chunk-{i}" for i in range(5)]
        assert [c["payload"] for c in b] == [f"chunk-{i}" for i in range(8)]
