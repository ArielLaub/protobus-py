"""MessageService and ServiceProxy against a real broker."""

import asyncio

import pytest

from protobus import Context, HandledError, MessageService, MessageServiceOptions, RemoteError, RetryOptions, ServiceProxy

from .conftest import unique

PKG = unique("Simple")
PROTO = f"""syntax = "proto3";
package {PKG};
message Request {{ int32 num1 = 1; int32 num2 = 2; }}
message Response {{ int32 result = 1; }}
message Event {{ string message = 1; }}
message MultiEvent {{ int32 count = 1; }}
service Service {{ rpc simpleMethod({PKG}.Request) returns({PKG}.Response); }}"""


class SimpleService(MessageService):
    service_name = f"{PKG}.Service"
    proto_file_name = ""
    Proto = PROTO

    def __init__(self, context):
        # A short retry delay so the invalid_params path (an unhandled error,
        # retried before the caller hears) finishes inside the test budget.
        super().__init__(context, MessageServiceOptions(max_concurrent=1, retry=RetryOptions(max_retries=2, retry_delay_ms=100)))

    async def simpleMethod(self, request, actor, correlation_id):
        if not request.get("num1") or not request.get("num2"):
            raise RuntimeError("invalid_params")
        return {"result": request["num1"] + request["num2"]}


@pytest.fixture
async def stack(amqp_url, cleanup_queues):
    context = Context()
    await context.init(amqp_url, [])
    service = SimpleService(context)
    await service.init()
    cleanup_queues.extend([f"{PKG}.Service", f"{PKG}.Service.Retry", f"{PKG}.Service.DLQ", f"{PKG}.Service.Events"])
    client = ServiceProxy(context, service.service_name)
    await client.init()
    yield context, service, client
    await service.close()
    await context.close()


class TestMessageService:
    async def test_an_rpc_call(self, stack):
        _, _, client = stack
        assert await client.simpleMethod({"num1": 1, "num2": 2}) == {"result": 3}

    async def test_an_event_call(self, stack):
        _, service, _ = stack
        received = asyncio.get_running_loop().create_future()

        async def handler(event, event_type, topic):
            if not received.done():
                received.set_result((event, event_type, topic))

        await service.subscribe_event(f"{PKG}.Event", handler)
        await service.publish_event(f"{PKG}.Event", {"message": "hello"})
        assert await asyncio.wait_for(received, 5) == ({"message": "hello"}, f"{PKG}.Event", f"EVENT.{PKG}.Event")

    async def test_star_wildcard_subscriptions(self, stack):
        _, service, _ = stack
        counts = []
        done = asyncio.get_running_loop().create_future()

        async def handler(event, event_type, topic):
            counts.append((event["count"], topic))
            if len(counts) == 2 and not done.done():
                done.set_result(None)

        await service.subscribe_event(f"{PKG}.MultiEvent", handler, "CUSTOM.*.TOPIC")
        await service.publish_event(f"{PKG}.MultiEvent", {"count": 1}, "CUSTOM.1.TOPIC")
        await service.publish_event(f"{PKG}.MultiEvent", {"count": 2}, "CUSTOM.2.TOPIC")
        await asyncio.wait_for(done, 5)
        assert sorted(counts) == [(1, "CUSTOM.1.TOPIC"), (2, "CUSTOM.2.TOPIC")]

    async def test_error_exceptions_flow_back_to_the_client(self, stack):
        _, _, client = stack
        # A plain exception is an infrastructure failure: retried max_retries
        # times, then the encoded error is published to the caller from the
        # DLQ path.
        with pytest.raises(RemoteError, match="invalid_params"):
            await client.simpleMethod({"no": "yes"}, timeout_ms=10000)

    async def test_the_python_typing_export(self, stack):
        context, _, _ = stack
        source = context.factory.export_python(f"{PKG}.Service")
        assert f'SERVICE_NAME = "{PKG}.Service"' in source
        assert "class Request(TypedDict, total=False):" in source
        assert "    num1: int" in source
        assert 'async def simpleMethod(self, request: "Request"' in source


class TestInstanceNamedServices:
    async def test_two_instances_share_one_contract(self, amqp_url, cleanup_queues):
        pkg = unique("Combat")
        proto = f'syntax = "proto3"; package {pkg}; message Req {{ string target = 1; }} message Res {{ string by = 1; }} service Player {{ rpc shoot(Req) returns (Res); }}'

        def player(name):
            class Player(MessageService):
                service_name = f"{pkg}.Player.{name}"
                proto_file_name = ""
                Proto = proto

                async def shoot(self, req, actor, cid):
                    return {"by": name}

            return Player

        context = Context()
        await context.init(amqp_url, [])
        try:
            p1 = player("player1")(context, retry=RetryOptions(max_retries=0))
            p2 = player("player2")(context, retry=RetryOptions(max_retries=0))
            await p1.init()
            await p2.init()
            cleanup_queues.extend([f"{pkg}.Player.player1", f"{pkg}.Player.player1.Events", f"{pkg}.Player.player2", f"{pkg}.Player.player2.Events"])
            assert p1.contract_service_name == f"{pkg}.Player"
            c1 = ServiceProxy(context, f"{pkg}.Player.player1")
            c2 = ServiceProxy(context, f"{pkg}.Player.player2")
            await c1.init()
            await c2.init()
            assert await c1.shoot({"target": "x"}) == {"by": "player1"}
            assert await c2.shoot({"target": "x"}) == {"by": "player2"}
            await p1.close()
            await p2.close()
        finally:
            await context.close()


class TestHandledErrorClass:
    def test_has_correct_properties(self):
        error = HandledError("test message", "TEST_CODE")
        assert error.message == "test message"
        assert error.code == "TEST_CODE"
        assert error.is_handled is True
        assert isinstance(error, Exception)

    def test_defaults_code_to_handled_error(self):
        assert HandledError("test").code == "HANDLED_ERROR"
