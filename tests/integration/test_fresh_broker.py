"""A process that only publishes must work on a broker nothing else has
touched: an event with no subscribers is a normal outcome, not a NOT_FOUND
that closes the publisher's channel."""

import asyncio

import pytest

from protobus import Context, MessageService, ServiceProxy, UnroutableError

from . import mgmt
from .conftest import unique

PKG = unique("Fresh")
PROTO = f"""
syntax = "proto3";
package {PKG};
message Ping {{ string id = 1; }}
message Req {{ string text = 1; }}
message Res {{ string text = 1; }}
service Svc {{ rpc echo({PKG}.Req) returns ({PKG}.Res); }}
"""


class Svc(MessageService):
    service_name = f"{PKG}.Svc"
    proto_file_name = ""
    Proto = PROTO

    async def echo(self, req, *_a):
        return {"text": f"echo:{req['text']}"}


class TestAPublisherOnAnEmptyVhost:
    async def test_events_and_requests_publish_before_any_service_exists(self, fresh_vhost):
        context = Context()
        await context.init(fresh_vhost, [])
        context.factory.parse(PROTO, f"{PKG}.Svc")
        try:
            # Nothing has declared proto.bus.events: the publisher must have.
            await context.publish_event(f"{PKG}.Ping", {"id": "first"})
            # The same channel is still usable afterwards.
            await context.publish_event(f"{PKG}.Ping", {"id": "second"}, "EVENT.custom.topic")

            # Nothing has declared proto.bus either. A request to a service
            # nobody runs is UNROUTABLE, not a closed channel.
            proxy = ServiceProxy(context, f"{PKG}.Svc")
            await proxy.init()
            with pytest.raises(UnroutableError):
                await proxy.echo({"text": "x"}, None, True, 2000)

            # Now a subscriber and a service arrive, on the same context, and
            # both publish paths keep working.
            received = asyncio.get_running_loop().create_future()
            service = Svc(context)
            await service.init()

            async def on_ping(event, *_a):
                if not received.done():
                    received.set_result(event["id"])

            await service.subscribe_event(f"{PKG}.Ping", on_ping)
            await context.publish_event(f"{PKG}.Ping", {"id": "third"})
            assert await asyncio.wait_for(received, 5) == "third"
            assert await proxy.echo({"text": "y"}, None, True, 5000) == {"text": "echo:y"}
            await service.close()
        finally:
            await context.close()

    async def test_the_publisher_still_works_after_a_connection_is_restored(self, fresh_vhost):
        context = Context()
        await context.init(fresh_vhost, [])
        context.factory.parse(PROTO, f"{PKG}.Svc")
        vhost = fresh_vhost.rsplit("/", 1)[1]
        try:
            await context.publish_event(f"{PKG}.Ping", {"id": "before"})
            reconnected = asyncio.get_running_loop().create_future()
            context.connection.on("reconnected", lambda *_: reconnected.done() or reconnected.set_result(True))
            assert await mgmt.kill_connections(vhost) >= 1
            await asyncio.wait_for(reconnected, 30)
            # The restored channel re-declared its exchange (the vhost is
            # unchanged here, but the restore path must not assume that).
            await context.publish_event(f"{PKG}.Ping", {"id": "after"})
        finally:
            await context.close()


RETRY_PKG = unique("Ladder")
RETRY_PROTO = f"""
syntax = "proto3";
package {RETRY_PKG};
message Req {{ string text = 1; }}
message Res {{ string text = 1; }}
service Svc {{ rpc work({RETRY_PKG}.Req) returns ({RETRY_PKG}.Res); }}
"""


class Flaky(MessageService):
    service_name = f"{RETRY_PKG}.Svc"
    proto_file_name = ""
    Proto = RETRY_PROTO

    def __init__(self, context):
        from protobus import MessageServiceOptions, RetryOptions

        super().__init__(context, MessageServiceOptions(retry=RetryOptions(max_retries=1, retry_delay_ms=200)))
        self.calls = []

    async def work(self, req, actor, correlation_id, ctx):
        self.calls.append((req["text"], ctx.message_id, ctx.routing_key))
        if req["text"].startswith("fail"):
            raise RuntimeError("synthetic failure")
        return {"text": f"done:{req['text']}"}


async def get_one(amqp_url, queue, attempts=60):
    import aiormq

    conn = await aiormq.connect(amqp_url)
    try:
        ch = await conn.channel()
        for _ in range(attempts):
            got = await ch.basic_get(queue, no_ack=True)
            if got.body:
                return got
            await asyncio.sleep(0.1)
        return None
    finally:
        await conn.close()


class TestTopologyLostDuringAnOutageIsRestored:
    async def test_retry_and_dlq_objects_deleted_during_the_outage_are_declared_again(self, fresh_vhost):
        from protobus import ContextOptions, ReconnectionOptions, RemoteError

        vhost = fresh_vhost.rsplit("/", 1)[1]
        context = Context()
        await context.init(fresh_vhost, [], ContextOptions(reconnection=ReconnectionOptions(max_retries=0, initial_delay_ms=300)))
        context.factory.parse(RETRY_PROTO, f"{RETRY_PKG}.Svc")
        service = Flaky(context)
        await service.init()
        proxy = ServiceProxy(context, service.service_name)
        await proxy.init()
        base = service.service_name
        try:
            assert (await proxy.work({"text": "one"}))["text"] == "done:one"

            reconnected = asyncio.get_running_loop().create_future()
            context.connection.on("reconnected", lambda *_: reconnected.done() or reconnected.set_result(True))
            await mgmt.kill_connections(vhost)
            # While it is down, the auxiliary topology disappears: a replaced
            # broker, a recreated vhost, an operator's delete.
            for queue in (f"{base}.Retry", f"{base}.DLQ"):
                await mgmt.run_blocking(mgmt.api, f"/api/queues/{mgmt.urllib.parse.quote(vhost, safe='')}/{queue}", "DELETE")
            await mgmt.run_blocking(mgmt.api, f"/api/exchanges/{mgmt.urllib.parse.quote(vhost, safe='')}/{base}.Retry.Exchange", "DELETE")
            await asyncio.wait_for(reconnected, 30)

            # The ladder works again: one retry hop, then the DLQ, with the
            # original routing key and a stable message id throughout.
            with pytest.raises(RemoteError, match="synthetic failure"):
                await proxy.work({"text": "fail-after"}, None, True, 15000)
            assert [c[0] for c in service.calls if c[0] == "fail-after"] == ["fail-after", "fail-after"]
            ids = {c[1] for c in service.calls if c[0] == "fail-after"}
            assert len(ids) == 1
            assert all(c[2] == f"REQUEST.{base}.work" for c in service.calls)
            dead = await get_one(fresh_vhost, f"{base}.DLQ")
            assert dead is not None
            assert dead.header.properties.headers["x-original-routing-key"] == f"REQUEST.{base}.work"
            assert dead.header.properties.message_id in ids
        finally:
            await service.close()
            await context.close()


class TestAChannelLostOnALiveConnectionIsRebuilt:
    async def test_a_missing_retry_exchange_closes_the_channel_and_the_listener_recovers(self, fresh_vhost):
        from protobus import RemoteError

        vhost = fresh_vhost.rsplit("/", 1)[1]
        context = Context()
        await context.init(fresh_vhost, [])
        context.factory.parse(RETRY_PROTO, f"{RETRY_PKG}.Svc")
        service = Flaky(context)
        await service.init()
        proxy = ServiceProxy(context, service.service_name)
        await proxy.init()
        base = service.service_name
        try:
            # No outage: the retry exchange is simply gone. The first failed
            # request's retry publish is a 404 that closes the consumer's
            # channel; the broker requeues the delivery, the listener rebuilds
            # its channel (re-declaring the exchange), and the redelivered
            # request climbs the ladder normally.
            await mgmt.run_blocking(mgmt.api, f"/api/exchanges/{mgmt.urllib.parse.quote(vhost, safe='')}/{base}.Retry.Exchange", "DELETE")
            with pytest.raises(RemoteError, match="synthetic failure"):
                await proxy.work({"text": "fail-live"}, None, True, 20000)
            attempts = [c for c in service.calls if c[0] == "fail-live"]
            # The redelivery after the channel loss, plus one retry hop.
            assert len(attempts) >= 2
            assert len({c[1] for c in attempts}) == 1
            assert (await proxy.work({"text": "after"}, None, True, 10000))["text"] == "done:after"
            dead = await get_one(fresh_vhost, f"{base}.DLQ")
            assert dead is not None
        finally:
            await service.close()
            await context.close()
