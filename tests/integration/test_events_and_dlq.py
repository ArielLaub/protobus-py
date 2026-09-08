"""Event failure semantics (default: drop), the opt-in event retry ladder,
and what an operator reads off a dead-lettered message."""

import asyncio

import aiormq
import pytest

from protobus import CallOptions, Context, EventRetryOptions, MessageService, MessageServiceOptions, RemoteError, RetryOptions, ServiceProxy

from .conftest import unique

EVT_PROTO = 'syntax = "proto3"; package EvtSem; message Ping { string id = 1; } service Sink {}'


async def queue_stats(amqp_url, name):
    """(message_count, consumer_count), or None if the queue does not exist."""
    conn = await aiormq.connect(amqp_url)
    try:
        ch = await conn.channel()
        try:
            ok = await ch.queue_declare(name, passive=True)
            return ok.message_count, ok.consumer_count
        except Exception:
            return None
    finally:
        await conn.close()


async def get_one(amqp_url, queue, attempts=40):
    conn = await aiormq.connect(amqp_url)
    try:
        ch = await conn.channel()
        for _ in range(attempts):
            got = await ch.basic_get(queue, no_ack=True)
            if got.body:
                return got
            await asyncio.sleep(0.05)
        return None
    finally:
        await conn.close()


class TestAnEventHandlerThatRaises:
    async def test_default_semantics_drop_the_event_without_stalling_the_consumer(self, amqp_url, cleanup_queues):
        svc_name = unique("EvtSem.Sink.run")

        class Sink(MessageService):
            proto_file_name = ""
            Proto = EVT_PROTO

            @property
            def service_name(self):
                return svc_name

        context = Context()
        await context.init(amqp_url, [])
        context.factory.parse(EVT_PROTO, "EvtSem.Sink")
        sink = Sink(context, retry=RetryOptions(max_retries=0))
        await sink.init()
        cleanup_queues.extend([svc_name, f"{svc_name}.Events"])
        failed, succeeded = [], []
        bad_topic, good_topic = unique("EVENT.evtsem.bad"), unique("EVENT.evtsem.good")

        async def bad(event, *_a):
            failed.append(event["id"])
            raise RuntimeError(f"handler failed for {event['id']}")

        async def good(event, *_a):
            succeeded.append(event["id"])

        await sink.subscribe_event("EvtSem.Ping", bad, bad_topic)
        await sink.subscribe_event("EvtSem.Ping", good, good_topic)
        try:
            # More events than the default prefetch (1), so an unacknowledged
            # message would visibly stall the consumer.
            for i in range(5):
                await context.publish_event("EvtSem.Ping", {"id": f"bad-{i}"}, bad_topic)
            await asyncio.sleep(1.5)
            await context.publish_event("EvtSem.Ping", {"id": "good-1"}, good_topic)
            for _ in range(40):
                if succeeded:
                    break
                await asyncio.sleep(0.05)

            # Delivered exactly once, not retried.
            assert failed == [f"bad-{i}" for i in range(5)]
            # Discarded: nothing left on the queue, no DLQ at all.
            assert await queue_stats(amqp_url, f"{svc_name}.Events") == (0, 1)
            assert await queue_stats(amqp_url, f"{svc_name}.Events.DLQ") is None
            # The consumer is not stalled.
            assert succeeded == ["good-1"]
        finally:
            await sink.close()
            await context.close()


class TestEventRetryWhenOptedIn:
    async def test_the_ladder(self, amqp_url, cleanup_queues):
        stamp = unique("")
        retrying_name = f"EvtRetry.Sink.retrying{stamp}"
        bystander_name = f"EvtRetry.Sink.bystander{stamp}"
        proto = 'syntax = "proto3"; package EvtRetry; message Ping { string id = 1; } service Sink {}'
        topic_transient, topic_always, topic_pair = f"EVENT.evtretry{stamp}.transient", f"EVENT.evtretry{stamp}.always", f"EVENT.evtretry{stamp}.pair"

        def sink_class(name, options):
            class Sink(MessageService):
                proto_file_name = ""
                Proto = proto

                @property
                def service_name(self):
                    return name

                def __init__(self, context):
                    super().__init__(context, options)

            return Sink

        context = Context()
        await context.init(amqp_url, [])
        context.factory.parse(proto, "EvtRetry.Sink")
        retrying = sink_class(retrying_name, MessageServiceOptions(event_retry=EventRetryOptions(max_retries=2, retry_delay_ms=300), retry=RetryOptions(max_retries=0)))(context)
        bystander = sink_class(bystander_name, MessageServiceOptions(retry=RetryOptions(max_retries=0)))(context)
        await retrying.init()
        await bystander.init()
        for svc in (retrying_name, bystander_name):
            cleanup_queues.extend([svc, f"{svc}.Events", f"{svc}.Events.Retry", f"{svc}.Events.DLQ"])

        transient_attempts, always_attempts, bystander_saw, first_of_pair, second_of_pair = [], [], [], [], []

        async def transient(event, *_a):
            transient_attempts.append(event["id"])
            if transient_attempts.count(event["id"]) == 1:
                raise RuntimeError(f"transient failure for {event['id']}")

        async def always(event, *_a):
            always_attempts.append(event["id"])
            raise RuntimeError(f"permanent failure for {event['id']}")

        async def first(event, *_a):
            first_of_pair.append(event["id"])

        async def second(event, *_a):
            second_of_pair.append(event["id"])
            if second_of_pair.count(event["id"]) == 1:
                raise RuntimeError(f"pair failure for {event['id']}")

        async def saw(event, *_a):
            bystander_saw.append(event["id"])

        await retrying.subscribe_event("EvtRetry.Ping", transient, topic_transient)
        await retrying.subscribe_event("EvtRetry.Ping", always, topic_always)
        await retrying.subscribe_event("EvtRetry.Ping", first, topic_pair)
        await retrying.subscribe_event("EvtRetry.Ping", second, topic_pair)
        await bystander.subscribe_event("EvtRetry.Ping", saw, topic_always)

        # Snoop the original publish so the DLQ copy's message id can be
        # compared against it.
        snoop = await aiormq.connect(amqp_url)
        snoop_ch = await snoop.channel()
        snoop_queue = (await snoop_ch.queue_declare("", exclusive=True)).queue
        await snoop_ch.queue_bind(snoop_queue, "proto.bus.events", topic_always)
        published_ids = []

        async def on_snoop(msg):
            published_ids.append(msg.header.properties.message_id)

        await snoop_ch.basic_consume(snoop_queue, on_snoop, no_ack=True)
        try:
            await context.publish_event("EvtRetry.Ping", {"id": "transient-1"}, topic_transient)
            await context.publish_event("EvtRetry.Ping", {"id": "always-1"}, topic_always)
            await context.publish_event("EvtRetry.Ping", {"id": "pair-1"}, topic_pair)
            # 2 retries at 300ms, plus handling time on each hop.
            await asyncio.sleep(3)

            assert transient_attempts == ["transient-1", "transient-1"]
            assert always_attempts == ["always-1", "always-1", "always-1"]
            dead = await get_one(amqp_url, f"{retrying_name}.Events.DLQ")
            assert dead is not None
            headers = dead.header.properties.headers
            assert headers["x-retry-count"] == 2
            assert headers["x-original-routing-key"] == topic_always
            assert headers["x-last-error"] == "RuntimeError"
            # Identity kept across every hop.
            assert published_ids and dead.header.properties.message_id == published_ids[0]
            # The redelivery is confined to the subscriber that failed.
            assert bystander_saw == ["always-1"]
            # Every handler that matched re-runs, not only the one that raised.
            assert second_of_pair == ["pair-1", "pair-1"]
            assert first_of_pair == ["pair-1", "pair-1"]
            assert await queue_stats(amqp_url, f"{retrying_name}.Events") == (0, 1)
        finally:
            await snoop.close()
            await retrying.close()
            await bystander.close()
            await context.close()


class TestDeadLetterMetadata:
    async def test_what_an_operator_reads_off_a_dead_lettered_message(self, amqp_url, cleanup_queues):
        service_name = unique("AuditWire.Service.run")
        proto = 'syntax = "proto3"; package AuditWire; message Request { string action = 1; } message Response { string result = 1; } service Service { rpc slowMethod(AuditWire.Request) returns(AuditWire.Response); }'

        class SlowService(MessageService):
            proto_file_name = ""
            Proto = proto

            @property
            def service_name(self):
                return service_name

            def __init__(self, context):
                # Shorter than the handler, so the connection layer's own
                # TimeoutError is what fails the delivery.
                super().__init__(context, MessageServiceOptions(processing_timeout_ms=150, retry=RetryOptions(max_retries=1, retry_delay_ms=100)))

            async def slowMethod(self, *_a):
                await asyncio.sleep(3)
                return {"result": "too late"}

        context = Context()
        await context.init(amqp_url, [])
        context.factory.parse(proto, "AuditWire.Service")
        service = SlowService(context)
        await service.init()
        cleanup_queues.extend([service_name, f"{service_name}.Retry", f"{service_name}.DLQ", f"{service_name}.Events"])
        proxy = ServiceProxy(context, service_name)
        await proxy.init()
        caller_message_id = unique("audit-wire-")
        try:
            # Unlike the TypeScript port, the caller is told about the timeout
            # (after the retry ladder) rather than left to wait out its own.
            with pytest.raises(RemoteError) as info:
                await proxy.slowMethod({"action": "x"}, None, True, 10000, CallOptions(message_id=caller_message_id))
            assert "processing timeout" in info.value.message
            assert info.value.code == "PROCESSING_TIMEOUT"
            dead = await get_one(amqp_url, f"{service_name}.DLQ")
            assert dead is not None
            props = dead.header.properties
            assert props.headers["x-last-error"] == "TimeoutError[PROCESSING_TIMEOUT]"
            assert props.content_type == "application/octet-stream"
            assert props.message_id == caller_message_id
            assert props.headers["x-retry-count"] == 1
            assert props.headers["x-original-queue"] == service_name
            assert props.reply_to is None
        finally:
            await service.close()
            await context.close()
