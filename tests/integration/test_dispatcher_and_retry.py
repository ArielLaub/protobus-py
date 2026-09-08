"""MessageDispatcher publish/confirm behaviour and the retry ladder against a
real broker."""

import asyncio
import time

import pytest

from protobus import (
    Config,
    Connection,
    Context,
    HandledError,
    MessageDispatcher,
    MessageService,
    MessageServiceOptions,
    PublishError,
    RemoteError,
    RetryOptions,
    ServiceProxy,
    UnroutableError,
)
from protobus.connection import ConsumeOptions

from .conftest import unique


@pytest.fixture
async def connection(amqp_url):
    conn = Connection()
    await conn.connect(amqp_url)
    yield conn
    await conn.disconnect()


class TestMessageDispatcher:
    async def test_publishes_rpc_and_waits_for_the_result(self, connection):
        channel = await connection.open_channel()
        await connection.declare_exchange(channel, Config.bus_exchange_name(), "topic", {"durable": True})
        dispatcher = MessageDispatcher(connection)
        await dispatcher.init()
        routing_key = unique("TEST.SERVICE.METHOD")
        queue = await connection.declare_queue(channel, "", {"durable": False, "exclusive": True, "auto_delete": True})
        await connection.bind_queue(channel, queue, Config.bus_exchange_name(), routing_key)

        async def handler(content, correlation_id, headers, context):
            assert content == b"test content"
            assert context.routing_key == routing_key
            return b"test result"

        await connection.consume(channel, queue, handler, ConsumeOptions(), True)
        assert await dispatcher.publish(b"test content", routing_key, True) == b"test result"
        await dispatcher.close()

    async def test_does_not_wait_for_a_result_on_non_rpc(self, connection):
        channel = await connection.open_channel()
        await connection.declare_exchange(channel, Config.bus_exchange_name(), "topic", {"durable": True})
        dispatcher = MessageDispatcher(connection)
        await dispatcher.init()
        routing_key = unique("TEST.SERVICE.METHOD2")
        queue = await connection.declare_queue(channel, "", {"durable": False, "exclusive": True, "auto_delete": True})
        await connection.bind_queue(channel, queue, Config.bus_exchange_name(), routing_key)
        processed = asyncio.Event()
        state = {"finished": False}

        async def handler(content, *_a):
            assert content == b"fire and forget"
            await asyncio.sleep(0.5)
            state["finished"] = True
            processed.set()
            return b"going nowhere"

        await connection.consume(channel, queue, handler, ConsumeOptions(), True)
        started = time.time()
        assert await dispatcher.publish(b"fire and forget", routing_key, False) is None
        assert time.time() - started < 0.5
        assert state["finished"] is False
        await asyncio.wait_for(processed.wait(), 5)
        await dispatcher.close()

    async def test_fails_fast_with_unroutable_error_when_no_service_is_bound(self, connection):
        channel = await connection.open_channel()
        await connection.declare_exchange(channel, Config.bus_exchange_name(), "topic", {"durable": True})
        dispatcher = MessageDispatcher(connection)
        await dispatcher.init()
        started = time.time()
        with pytest.raises(UnroutableError) as info:
            await dispatcher.publish(b"nobody is listening", "TEST.SERVICE.NOTHING.IS.BOUND.HERE", True, 20000)
        assert isinstance(info.value, PublishError)
        assert time.time() - started < 5
        assert isinstance(info.value.message_id, str)
        # The reply slot is released when the request never landed.
        assert len(dispatcher.pending_callbacks) == 0
        await dispatcher.close()

    async def test_an_event_with_no_subscribers_is_not_an_error(self, amqp_url):
        context = Context()
        await context.init(amqp_url, [])
        context.factory.parse('syntax = "proto3"; package Lonely; message Ev { string x = 1; }', None)
        try:
            await context.publish_event("Lonely.Ev", {"x": "nobody"}, unique("EVENT.nobody."))
        finally:
            await context.close()


PKG = unique("Retry")
PROTO = f"""syntax = "proto3";
package {PKG};
message Request {{ string action = 1; }}
message Response {{ string result = 1; }}
service Service {{ rpc testMethod({PKG}.Request) returns({PKG}.Response); }}"""


class RetryTestService(MessageService):
    service_name = f"{PKG}.Service"
    proto_file_name = ""
    Proto = PROTO

    def __init__(self, context, max_retries=3, retry_delay_ms=100):
        super().__init__(context, MessageServiceOptions(max_concurrent=1, retry=RetryOptions(max_retries=max_retries, retry_delay_ms=retry_delay_ms)))
        self.calls = []
        self.fail_count = 0
        self.max_fails = 0
        self.seen_contexts = []

    def set_fail_behavior(self, max_fails):
        self.fail_count = 0
        self.max_fails = max_fails

    async def testMethod(self, request, actor, correlation_id, context):
        self.calls.append(request["action"])
        self.seen_contexts.append(context)
        if request["action"] == "handled_error":
            raise HandledError("This is a handled validation error", "VALIDATION_ERROR")
        if request["action"] == "unhandled_error":
            if self.fail_count < self.max_fails:
                self.fail_count += 1
                raise RuntimeError("Temporary database error")
            return {"result": "recovered"}
        if request["action"] == "always_fail":
            raise RuntimeError("Permanent failure")
        return {"result": "success"}


@pytest.fixture
async def retry_stack(amqp_url, cleanup_queues):
    context = Context()
    await context.init(amqp_url, [])
    service = RetryTestService(context, 3, 100)
    await service.init()
    cleanup_queues.extend([f"{PKG}.Service", f"{PKG}.Service.Retry", f"{PKG}.Service.DLQ", f"{PKG}.Service.Events"])
    client = ServiceProxy(context, f"{PKG}.Service")
    await client.init()
    yield context, service, client
    await service.close()
    await context.close()


class TestRetryAndDlq:
    async def test_does_not_retry_when_a_handled_error_is_raised(self, retry_stack):
        _, service, client = retry_stack
        with pytest.raises(RemoteError) as info:
            await client.testMethod({"action": "handled_error"})
        assert info.value.message == "This is a handled validation error"
        assert info.value.code == "VALIDATION_ERROR"
        await asyncio.sleep(0.5)
        assert service.calls.count("handled_error") == 1

    async def test_retries_unhandled_errors_and_succeeds_after_recovery(self, retry_stack):
        _, service, client = retry_stack
        service.set_fail_behavior(2)
        started = time.time()
        assert await client.testMethod({"action": "unhandled_error"}) == {"result": "recovered"}
        # Initial + 2 retries that failed + 1 success.
        assert service.calls.count("unhandled_error") == 3
        assert time.time() - started >= 0.2  # two 100ms hops
        # The identity survived every hop.
        ids = {c.message_id for c in service.seen_contexts}
        assert len(ids) == 1
        # A retried delivery arrives on the original routing key.
        assert all(c.routing_key == f"REQUEST.{PKG}.Service.testMethod" for c in service.seen_contexts)

    async def test_sends_to_the_dlq_after_max_retries_are_exceeded(self, retry_stack, amqp_url):
        context, service, client = retry_stack
        with pytest.raises(RemoteError, match="Permanent failure"):
            await client.testMethod({"action": "always_fail"}, timeout_ms=10000)
        assert service.calls.count("always_fail") == 4  # initial + 3 retries

        # The message is on the DLQ with its metadata.
        import aiormq

        conn = await aiormq.connect(amqp_url)
        ch = await conn.channel()
        # The caller is answered BEFORE the DLQ publish, so give it a moment.
        for _ in range(50):
            dead = await ch.basic_get(f"{PKG}.Service.DLQ", no_ack=True)
            if dead.body:
                break
            await asyncio.sleep(0.05)
        await conn.close()
        assert dead.body
        headers = dead.header.properties.headers
        assert headers["x-retry-count"] == 3
        assert headers["x-original-routing-key"] == f"REQUEST.{PKG}.Service.testMethod"
        assert headers["x-original-queue"] == f"{PKG}.Service"
        assert headers["x-last-error"] == "RuntimeError"
        assert "x-dlq-time" in headers and "x-first-failure-time" in headers
        assert dead.header.properties.content_type == "application/octet-stream"
        assert dead.header.properties.delivery_mode == 2

    async def test_does_not_retry_on_success(self, retry_stack):
        _, service, client = retry_stack
        assert await client.testMethod({"action": "normal"}) == {"result": "success"}
        await asyncio.sleep(0.2)
        assert service.calls.count("normal") == 1

    async def test_requests_are_persistent_on_the_wire(self, retry_stack, amqp_url):
        context, service, client = retry_stack
        # Stop the service's consumer so the request sits on the queue.
        await service.stop_consuming()
        task = asyncio.ensure_future(client.testMethod({"action": "normal"}, timeout_ms=5000))
        await asyncio.sleep(0.3)
        import aiormq

        conn = await aiormq.connect(amqp_url)
        ch = await conn.channel()
        parked = await ch.basic_get(f"{PKG}.Service", no_ack=True)
        await conn.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert parked is not None
        assert parked.header.properties.delivery_mode == 2
        assert parked.header.properties.message_id
