"""
Recovery across a real connection loss.

The socket is severed from the broker side through the management API, on a
vhost of this test's own, so only its connections are closed. Needs the
management plugin at ``RABBITMQ_MGMT`` (default http://guest:guest@localhost:15672);
skipped otherwise.
"""

import asyncio
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from protobus import Context, ContextOptions, LogLevel, MessageService, ReconnectionOptions, RetryOptions, ServiceProxy, get_log_level, set_log_level

from .conftest import unique

MGMT = os.environ.get("RABBITMQ_MGMT", "http://guest:guest@localhost:15672")
BASE = urllib.parse.urlsplit(MGMT)
ORIGIN = f"{BASE.scheme}://{BASE.hostname}:{BASE.port or 15672}"
AUTH = "Basic " + base64.b64encode(f"{BASE.username}:{BASE.password}".encode()).decode()
VHOST = unique("protobus-recovery-")

PKG = unique("Recov")
PROTO = f"""
syntax = "proto3";
package {PKG};
service Svc {{ rpc echo(Req) returns (Res); }}
message Req {{ string text = 1; }}
message Res {{ string text = 1; }}
"""


class Svc(MessageService):
    service_name = f"{PKG}.Svc"
    proto_file_name = "Recov.proto"
    Proto = PROTO

    async def echo(self, req, *_a):
        return {"text": f"echo:{req['text']}"}


def api(path, method="GET", body=None):
    request = urllib.request.Request(f"{ORIGIN}{path}", method=method, headers={"Authorization": AUTH, "Content-Type": "application/json"})
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(request, data=data, timeout=5) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def management_available():
    try:
        api("/api/overview")
        return True
    except Exception:
        return False


async def run_blocking(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


@pytest.fixture
async def vhost(amqp_url):
    if not await run_blocking(management_available):
        pytest.skip(f"RabbitMQ management API not reachable at {ORIGIN}")
    encoded = urllib.parse.quote(VHOST, safe="")
    await run_blocking(api, f"/api/vhosts/{encoded}", "PUT")
    await run_blocking(api, f"/api/permissions/{encoded}/{urllib.parse.quote(BASE.username, safe='')}", "PUT", {"configure": ".*", "write": ".*", "read": ".*"})
    parts = urllib.parse.urlsplit(amqp_url)
    url = f"amqp://{BASE.username}:{BASE.password}@{parts.hostname}:{parts.port or 5672}/{encoded}"
    yield url
    try:
        await run_blocking(api, f"/api/vhosts/{encoded}", "DELETE")
    except Exception:
        pass


def own_connections():
    return [c for c in api("/api/connections") if c.get("vhost") == VHOST]


async def wait_for_connections(minimum=1, timeout=30):
    deadline = time.time() + timeout
    while True:
        mine = await run_blocking(own_connections)
        if len(mine) >= minimum or time.time() > deadline:
            return mine
        await asyncio.sleep(0.5)


async def kill_own_connections():
    mine = await wait_for_connections()
    for conn in mine:
        await run_blocking(api, f"/api/connections/{urllib.parse.quote(conn['name'], safe='')}", "DELETE")
    return len(mine)


class TestTheConfiguredHeartbeatReachesTheBroker:
    async def test_is_what_the_connection_negotiates(self, vhost, monkeypatch):
        monkeypatch.setenv("AMQP_HEARTBEAT_SECONDS", "11")
        ctx = Context()
        await ctx.init(vhost, [])
        try:
            mine = await wait_for_connections()
            assert len(mine) == 1
            assert mine[0]["timeout"] == 11
        finally:
            await ctx.close()


class TestRecoveryAcrossARealConnectionLoss:
    async def test_parks_work_through_the_outage_and_serves_again_afterwards(self, vhost):
        level = get_log_level()
        set_log_level(LogLevel.Error)
        ctx = Context()
        await ctx.init(vhost, [], ContextOptions(reconnection=ReconnectionOptions(max_retries=0, initial_delay_ms=300, max_delay_ms=2000)))
        svc = Svc(ctx, retry=RetryOptions(max_retries=0))
        await svc.init()
        proxy = ServiceProxy(ctx, f"{PKG}.Svc")
        await proxy.init()
        try:
            assert (await proxy.echo({"text": "one"}))["text"] == "echo:one"

            seen = []
            ready_at_announce = {}
            disconnected = asyncio.Event()
            reconnected = asyncio.Event()

            def on_disconnected():
                seen.append("disconnected")
                disconnected.set()

            def on_reconnected():
                seen.append("reconnected")
                # By the time the announcement lands, the topology is back.
                ready_at_announce["value"] = ctx.connection.is_ready
                reconnected.set()

            ctx.connection.on("disconnected", on_disconnected)
            ctx.connection.on("reconnected", on_reconnected)

            assert await kill_own_connections() == 1

            # Issued into the gap, before the socket loss has been noticed.
            parked = asyncio.ensure_future(proxy.echo({"text": "parked"}, None, True, 120000))

            await asyncio.wait_for(disconnected.wait(), 30)
            await asyncio.wait_for(reconnected.wait(), 60)
            assert seen == ["disconnected", "reconnected"]
            assert ready_at_announce["value"] is True

            # A publish issued mid-outage is waited through, not failed.
            assert (await asyncio.wait_for(parked, 30))["text"] == "echo:parked"
            # And the restored topology actually serves new work.
            assert (await proxy.echo({"text": "after"}))["text"] == "echo:after"

            # The old generation is gone: exactly one connection on the vhost.
            await asyncio.sleep(1)
            assert len(await wait_for_connections()) == 1
        finally:
            set_log_level(level)
            await svc.stop_consuming()
            await ctx.close()

    async def test_events_and_streams_survive_a_reconnection(self, vhost):
        pkg = unique("RecovEv")
        proto = f'syntax = "proto3"; package {pkg}; message Ev {{ string x = 1; }} message Req {{ int32 n = 1; }} message Chunk {{ int32 i = 1; }} service S {{ rpc count(Req) returns (stream Chunk); }}'

        class S(MessageService):
            service_name = f"{pkg}.S"
            proto_file_name = ""
            Proto = proto

            async def count(self, req, *_a):
                for i in range(req["n"]):
                    yield {"i": i}

        level = get_log_level()
        set_log_level(LogLevel.Error)
        ctx = Context()
        await ctx.init(vhost, [], ContextOptions(reconnection=ReconnectionOptions(max_retries=0, initial_delay_ms=300, max_delay_ms=2000)))
        svc = S(ctx, retry=RetryOptions(max_retries=0))
        await svc.init()
        received = []

        async def on_event(data, event_type, topic):
            received.append(data["x"])

        await svc.subscribe_event(f"{pkg}.Ev", on_event)
        proxy = ServiceProxy(ctx, f"{pkg}.S")
        await proxy.init()
        try:
            reconnected = asyncio.Event()
            ctx.connection.on("reconnected", reconnected.set)
            await kill_own_connections()
            await asyncio.wait_for(reconnected.wait(), 60)

            await ctx.publish_event(f"{pkg}.Ev", {"x": "after"})
            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.1)
            assert received == ["after"]
            assert [c["i"] async for c in proxy.count({"n": 3})] == [0, 1, 2]
        finally:
            set_log_level(level)
            await svc.stop_consuming()
            await ctx.close()
