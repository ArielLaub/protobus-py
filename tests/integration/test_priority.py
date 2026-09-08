"""Message priority against a real broker, measured in message ORDER."""

import asyncio

import pytest

from protobus import CallOptions, Config, Context, MessageService, MessageServiceOptions, RetryOptions, ServiceProxy

from .conftest import unique

PREFETCH = 1


def proto_for(pkg):
    return f'syntax = "proto3"; package {pkg}; message Request {{ string tag = 1; }} message Response {{ string tag = 1; }} service Service {{ rpc handle({pkg}.Request) returns({pkg}.Response); }}'


def recording_service(pkg, max_priority=None):
    class RecordingService(MessageService):
        proto_file_name = ""

        @property
        def service_name(self):
            return f"{pkg}.Service"

        @property
        def Proto(self):
            return proto_for(pkg)

        def __init__(self, context):
            super().__init__(context, MessageServiceOptions(max_concurrent=PREFETCH, retry=RetryOptions(max_retries=0), max_priority=max_priority))
            self.handled = []
            self.gate = None
            self.first_entered = asyncio.Event()

        def arm_gate(self):
            self.gate = asyncio.Event()

        async def handle(self, request, *_a):
            if self.gate is not None and not self.first_entered.is_set():
                self.first_entered.set()
                await self.gate.wait()
            self.handled.append(request["tag"])
            return {"tag": request["tag"]}

    return RecordingService


async def build(amqp_url, cleanup_queues, pkg, max_priority=None):
    context = Context()
    await context.init(amqp_url, [])
    service = recording_service(pkg, max_priority)(context)
    service.arm_gate()
    await service.init()
    cleanup_queues.extend([f"{pkg}.Service", f"{pkg}.Service.Events"])
    proxy = ServiceProxy(context, f"{pkg}.Service")
    await proxy.init()
    return context, service, proxy


async def backlog_then_control(service, proxy, bulk=30):
    """Block the first delivery, queue a bulk backlog, then one control message."""
    calls = [asyncio.ensure_future(proxy.handle({"tag": "first"}, None, True, 30000))]
    await asyncio.wait_for(service.first_entered.wait(), 10)
    for i in range(bulk):
        calls.append(asyncio.ensure_future(proxy.handle({"tag": f"bulk-{i}"}, None, True, 30000)))
    # Let the backlog land on the queue before the control message.
    await asyncio.sleep(0.5)
    calls.append(asyncio.ensure_future(proxy.handle({"tag": "control"}, None, True, 30000, CallOptions(priority=Config.PRIORITY_CONTROL))))
    await asyncio.sleep(0.3)
    service.gate.set()
    await asyncio.gather(*calls)
    return service.handled.index("control")


class TestAPriorityQueueLetsAControlMessageOvertakeABulkBacklog:
    async def test_the_control_message_is_handled_ahead_of_the_backlog(self, amqp_url, cleanup_queues):
        pkg = unique("PrioHi")
        context, service, proxy = await build(amqp_url, cleanup_queues, pkg, Config.RECOMMENDED_MAX_PRIORITY)
        try:
            position = await backlog_then_control(service, proxy)
            # Only what was already prefetched can be ahead of it.
            assert position <= PREFETCH + 1
        finally:
            await service.close()
            await context.close()


class TestAPlainQueueIgnoresPriority:
    async def test_a_priority_published_to_a_plain_queue_is_accepted_and_ignored(self, amqp_url, cleanup_queues):
        pkg = unique("PrioPlain")
        context, service, proxy = await build(amqp_url, cleanup_queues, pkg, None)
        try:
            position = await backlog_then_control(service, proxy)
            # FIFO: the control message comes after the whole backlog.
            assert position == len(service.handled) - 1
        finally:
            await service.close()
            await context.close()


class TestEnablingPriorityOnAnExistingQueueNeedsAnOperator:
    async def test_adding_x_max_priority_to_an_existing_queue_is_refused_by_the_broker(self, amqp_url, cleanup_queues):
        pkg = unique("PrioMigrate")
        context = Context()
        await context.init(amqp_url, [])
        plain = recording_service(pkg)(context)
        await plain.init()
        cleanup_queues.extend([f"{pkg}.Service", f"{pkg}.Service.Events"])
        await plain.close()
        upgraded = recording_service(pkg, Config.RECOMMENDED_MAX_PRIORITY)(context)
        try:
            with pytest.raises(Exception) as info:
                await upgraded.init()
            assert "PRECONDITION_FAILED" in str(info.value) or "inequivalent" in str(info.value)
        finally:
            await context.close()
