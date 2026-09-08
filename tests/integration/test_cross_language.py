"""
Cross-language: Python client -> TypeScript server.

The TypeScript checkout (``PROTOBUS_TS``, default ``../protobus`` beside this
repo) must be built (``npm run build``) and have its dependencies installed;
otherwise this module is skipped. It is the mirror of the TypeScript repo's
``cross-language.test.ts``, which drives a Python server from a TS client.
"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from protobus import Context, RemoteError, ServiceProxy

from .conftest import RUN_ID

HERE = Path(__file__).resolve().parent
TS_REPO = Path(os.environ.get("PROTOBUS_TS", HERE.parents[2] / "protobus"))
SERVER = HERE / "cross_lang" / "ts_counter_server.js"
PROTO_DIR = HERE.parent / "streaming_proto"
SUFFIX = f"X{RUN_ID}"

pytestmark = pytest.mark.cross_language


@pytest.fixture(scope="module")
def ts_server(request):
    amqp_url = request.getfixturevalue("amqp_url")
    if not (TS_REPO / "dist" / "lib" / "context.js").exists():
        pytest.skip(f"TypeScript protobus not built at {TS_REPO} (set PROTOBUS_TS)")
    proc = subprocess.Popen(
        ["node", str(SERVER)],
        env={**os.environ, "PROTOBUS_TS": str(TS_REPO), "PROTOBUS_TEST_AMQP": amqp_url, "PROTOBUS_TEST_PROTO_DIR": str(PROTO_DIR), "PROTOBUS_TEST_SUFFIX": SUFFIX},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    line = proc.stdout.readline()
    if "READY" not in line:
        err = proc.stderr.read()
        proc.kill()
        pytest.skip(f"TypeScript server did not start: {line!r} {err[-500:]}")
    yield proc
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
async def stack(amqp_url, ts_server):
    context = Context()
    await context.init(amqp_url, [str(PROTO_DIR)])
    counter = ServiceProxy(context, "streaming_test.Counter")
    await counter.init()
    yield context, counter
    await context.close()


WALLET_PROTO = f"""syntax = "proto3";
package Wallet{SUFFIX};
message Balance {{ bigint amount = 1; timestamp as_of = 2; int64 big = 3; repeated string tags = 4; map<string, int32> counts = 5; }}
message Query {{ string account = 1; int32 zero = 2; }}
message Ping {{ string id = 1; bigint n = 2; }}
service Api {{ rpc balance (Query) returns (Balance); }}"""


class TestPythonClientToTypeScriptServer:
    async def test_unary_call(self, stack):
        _, counter = stack
        assert await counter.add({"a": 5, "b": 7}) == {"sum": 12}

    async def test_streaming_call_delivers_chunks_in_order(self, stack):
        _, counter = stack
        chunks = [c async for c in counter.tick({"count": 5})]
        assert [c["payload"] for c in chunks] == [f"chunk-{i}" for i in range(5)]
        assert [c["seq"] for c in chunks] == [0, 1, 2, 3, 4]

    async def test_empty_stream_ends_cleanly(self, stack):
        _, counter = stack
        assert [c async for c in counter.tick({"emit_nothing": True})] == []

    async def test_mid_stream_handled_error_propagates(self, stack):
        _, counter = stack
        chunks = []
        with pytest.raises(RemoteError) as info:
            async for c in counter.tick({"count": 10, "fail_at": 2}):
                chunks.append(c)
        assert len(chunks) == 2
        assert info.value.code == "TEST_FAIL"
        assert "deliberate failure" in info.value.message

    async def test_cancellation_reaches_the_typescript_producer(self, stack):
        _, counter = stack
        received = []
        async with counter.tick({"count": 500, "delay_ms": 10}) as stream:
            async for c in stream:
                received.append(c)
                if len(received) == 3:
                    break
        assert len(received) == 3

    async def test_custom_types_defaults_and_maps_cross_the_wire(self, stack):
        context, _ = stack
        context.factory.parse(WALLET_PROTO, f"Wallet{SUFFIX}.Api")
        wallet = ServiceProxy(context, f"Wallet{SUFFIX}.Api")
        await wallet.init()
        out = await wallet.balance({"account": "acc", "zero": 0})
        assert out["amount"] == 10**30
        assert out["as_of"] == datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert out["big"] == 9007199254740993
        assert out["tags"] == ["a", "b"]
        assert out["counts"] == {"x": 1, "y": 2}
        with pytest.raises(RemoteError) as info:
            await wallet.balance({"account": "boom"})
        assert info.value.code == "NOT_FOUND"

    async def test_events_cross_the_wire_in_both_directions(self, stack, cleanup_queues):
        context, _ = stack
        context.factory.parse(WALLET_PROTO, f"Wallet{SUFFIX}.Api")
        from protobus import MessageService, RetryOptions

        pkg = f"Wallet{SUFFIX}"

        class Listener(MessageService):
            service_name = f"{pkg}.PyListener"
            proto_file_name = ""
            Proto = f'syntax = "proto3"; package {pkg}; service PyListener {{}}'

        listener = Listener(context, retry=RetryOptions(max_retries=0))
        await listener.init()
        cleanup_queues.extend([f"{pkg}.PyListener", f"{pkg}.PyListener.Events"])
        got = asyncio.get_running_loop().create_future()

        async def on_pong(event, event_type, topic):
            if not got.done():
                got.set_result((event, event_type, topic))

        await listener.subscribe_event(f"{pkg}.Ping", on_pong, f"EVENT.pong{SUFFIX}")
        await context.publish_event(f"{pkg}.Ping", {"id": "py-1", "n": 2**70}, f"EVENT.ping{SUFFIX}")
        event, event_type, topic = await asyncio.wait_for(got, 10)
        assert event == {"id": "pong:py-1", "n": 2**70 + 1}
        assert (event_type, topic) == (f"{pkg}.Ping", f"EVENT.pong{SUFFIX}")
        await listener.close()
