"""
Tests for RabbitMQ message-priority support.

Two things are under test and they pull in opposite directions:

1. That priority WORKS — a queue declared with ``x-max-priority`` really does
   hand a high-priority message out ahead of a backlog of low-priority ones.
2. That priority is INVISIBLE unless asked for — a listener that does not opt
   in must declare its queue with byte-identical arguments to a listener from
   before this feature existed. Adding ``x-max-priority`` to a queue that
   already exists is a 406 PRECONDITION_FAILED, which closes that listener's
   channel and stops it starting — so a silent default would stop every
   already-deployed service from booting on upgrade. (It does NOT take down
   other listeners on the same connection; that is measured in
   test_a_406_does_not_take_down_other_listeners_on_the_connection.)

Guarantee 2 is the reason most of the assertions here are about arguments
being ABSENT rather than present.

The broker-backed tests need a live RabbitMQ:

    docker-compose up -d
    pytest tests/test_priority.py -v
"""

import asyncio
import struct
import uuid
from typing import Any, Dict, List, Optional

import aio_pika
import pytest
from aio_pika import ExchangeType, Message

from protobus import (
    Config,
    Connection,
    Context,
    MessageService,
    MessageServiceOptions,
    RetryOptions,
    ServiceProxy,
)
from protobus.base_listener import BaseListener
from protobus.errors import InvalidPriorityError, PublishMessageError
from protobus.message_listener import MessageListener


RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"


# --------------------------------------------------------------------------
# A fake connection that records exactly what would be declared on the broker.
#
# The point of these tests is the ARGUMENTS dict, not the broker's reaction to
# it, so a fake gives a sharper oracle than a live declare: it can assert
# "the key is not present" rather than "the broker didn't complain".
# --------------------------------------------------------------------------
class RecordingConnection:
    """Captures declare/consume calls made by a listener."""

    def __init__(self) -> None:
        self.queue_declares: List[Dict[str, Any]] = []
        self.published: List[Dict[str, Any]] = []
        self.is_connected = True
        self.is_reconnecting = False

    def on(self, event: str, callback: Any) -> None:
        pass

    async def open_channel(self) -> Any:
        return object()

    async def ensure_exchange(self, channel, name, exchange_type=None):
        return object()

    async def ensure_queue(self, channel, name, arguments=None):
        self.queue_declares.append({"name": name, "arguments": arguments})

        class _Q:
            pass

        q = _Q()
        q.name = name or "amq.gen-fake"
        return q

    async def bind_queue(self, queue, exchange, routing_key):
        pass

    async def consume(self, channel, queue, handler, **kwargs):
        return "consumer-tag"

    async def publish(self, channel, exchange, routing_key, body, properties=None):
        self.published.append({"routing_key": routing_key, "properties": properties})


def declare_for(name: str, connection: RecordingConnection) -> Dict[str, Any]:
    """The recorded declare for one queue name."""
    matches = [d for d in connection.queue_declares if d["name"] == name]
    assert matches, f"no declare recorded for {name!r} in {connection.queue_declares}"
    return matches[0]


# ==========================================================================
# 1. Queue declaration — opt-in only
# ==========================================================================
class TestQueueDeclaration:
    async def test_listener_without_max_priority_declares_no_arguments(self):
        """
        THE backward-compatibility guarantee.

        A listener that does not ask for priority must produce the same
        declare it produced before this feature existed: arguments None.
        Anything else 406s against an already-deployed queue.
        """
        conn = RecordingConnection()
        listener = BaseListener(conn)
        listener._exchange_name = "proto.bus"
        await listener.init(handler=lambda *a: None, queue_name="svc.NoPriority")

        assert declare_for("svc.NoPriority", conn)["arguments"] is None

    async def test_listener_with_max_priority_sets_x_max_priority(self):
        conn = RecordingConnection()
        listener = BaseListener(conn, late_ack=True, max_concurrent=1, max_priority=2)
        listener._exchange_name = "proto.bus"
        await listener.init(handler=lambda *a: None, queue_name="svc.Priority")

        assert declare_for("svc.Priority", conn)["arguments"] == {"x-max-priority": 2}

    async def test_max_priority_combines_with_message_ttl(self):
        conn = RecordingConnection()
        listener = BaseListener(
            conn, late_ack=True, max_concurrent=1, message_ttl_ms=60000, max_priority=2
        )
        listener._exchange_name = "proto.bus"
        await listener.init(handler=lambda *a: None, queue_name="svc.Both")

        assert declare_for("svc.Both", conn)["arguments"] == {
            "x-message-ttl": 60000,
            "x-max-priority": 2,
        }

    async def test_message_ttl_alone_is_unchanged(self):
        """A TTL-only listener must not acquire a priority argument."""
        conn = RecordingConnection()
        listener = BaseListener(conn, message_ttl_ms=60000)
        listener._exchange_name = "proto.bus"
        await listener.init(handler=lambda *a: None, queue_name="svc.Ttl")

        assert declare_for("svc.Ttl", conn)["arguments"] == {"x-message-ttl": 60000}

    async def test_message_listener_threads_max_priority_through(self):
        conn = RecordingConnection()
        listener = MessageListener(conn, late_ack=True, max_concurrent=1, max_priority=2)
        await listener.init(handler=lambda *a: None, queue_name="svc.Msg")

        assert declare_for("svc.Msg", conn)["arguments"] == {"x-max-priority": 2}

    async def test_retry_and_dlq_queues_never_get_max_priority(self):
        """
        Enabling priority is a ONE-queue migration, not three.

        A retried message keeps its `priority` property across the
        dead-letter hop (verified against a real broker in
        test_dead_lettering_preserves_priority), so it re-sorts correctly the
        moment it lands back on the main priority queue. Giving the .retry
        and .DLQ queues their own x-max-priority would buy nothing and would
        turn one operator delete-and-recreate into three.
        """
        conn = RecordingConnection()
        listener = MessageListener(
            conn,
            late_ack=True,
            max_concurrent=1,
            retry_options=RetryOptions(max_retries=3, retry_delay_ms=5000),
            max_priority=2,
        )
        await listener.init(handler=lambda *a: None, queue_name="svc.Retrying")
        await listener.subscribe("REQUEST.svc.Retrying.*")

        assert declare_for("svc.Retrying", conn)["arguments"] == {"x-max-priority": 2}
        assert "x-max-priority" not in (declare_for("svc.Retrying.DLQ", conn)["arguments"] or {})
        assert "x-max-priority" not in declare_for("svc.Retrying.retry", conn)["arguments"]


# ==========================================================================
# 1b. Priority is refused when it would silently do nothing
#
# Measured against a real broker, with 300 bulk messages arriving while the
# consumer is already draining and one control message published after them:
#
#   max_concurrent=1, late_ack=True   -> control handled at position 92/301
#   max_concurrent=1, late_ack=False  -> control handled at position 300/301
#   max_concurrent=None               -> control handled at position 300/301
#
# i.e. without a REAL prefetch bound, priority does nothing at all — the broker
# pushes the whole queue into the consumer's buffer and the control message is
# handled dead last. RabbitMQ ignores QoS prefetch for auto-ack consumers, so
# `max_concurrent` without `late_ack` is just as useless as no prefetch.
#
# That failure is invisible: the queue is correctly declared with
# x-max-priority, an operator has done the drain/delete/recreate migration, and
# nothing reports that the feature is inert. So it is refused at construction
# instead.
# ==========================================================================
class TestPriorityRequiresABoundedPrefetch:
    def test_max_priority_without_max_concurrent_is_refused(self):
        with pytest.raises(InvalidPriorityError) as e:
            BaseListener(RecordingConnection(), max_priority=2)
        assert "max_concurrent" in str(e.value)

    def test_max_priority_with_autoack_is_refused(self):
        """max_concurrent alone is not enough — auto-ack ignores prefetch."""
        with pytest.raises(InvalidPriorityError) as e:
            BaseListener(RecordingConnection(), max_concurrent=10, max_priority=2)
        assert "late_ack" in str(e.value)

    def test_max_priority_with_a_real_prefetch_is_accepted(self):
        listener = BaseListener(
            RecordingConnection(), late_ack=True, max_concurrent=10, max_priority=2
        )
        assert listener._max_priority == 2

    def test_no_priority_is_unaffected_by_any_of_this(self):
        """The default path must not acquire a new way to fail."""
        assert BaseListener(RecordingConnection())._max_priority is None
        assert BaseListener(RecordingConnection(), max_concurrent=10) is not None
        assert BaseListener(RecordingConnection(), late_ack=True) is not None

    def test_service_options_priority_without_max_concurrent_is_refused(self):
        class _Svc(MessageService):
            @property
            def service_name(self) -> str:
                return "test.BadOpts"

            @property
            def proto_file_name(self) -> str:
                return "test.proto"

        class _Ctx:
            connection = RecordingConnection()
            factory = None

        with pytest.raises(InvalidPriorityError):
            _Svc(_Ctx(), MessageServiceOptions(max_priority=2))

    def test_service_options_priority_with_max_concurrent_is_accepted(self):
        class _Svc(MessageService):
            @property
            def service_name(self) -> str:
                return "test.GoodOpts"

            @property
            def proto_file_name(self) -> str:
                return "test.proto"

        class _Ctx:
            connection = RecordingConnection()
            factory = None

        # MessageService derives late_ack from max_concurrent, so this is
        # all a user has to supply.
        svc = _Svc(_Ctx(), MessageServiceOptions(max_concurrent=10, max_priority=2))
        assert svc._listener._max_priority == 2


# ==========================================================================
# 2. Service options
# ==========================================================================
class TestServiceOptions:
    def test_default_max_priority_is_none(self):
        assert MessageServiceOptions().max_priority is None

    def test_max_priority_is_carried(self):
        assert MessageServiceOptions(max_concurrent=1, max_priority=2).max_priority == 2

    async def test_service_options_reach_the_queue_declare(self):
        class _Svc(MessageService):
            @property
            def service_name(self) -> str:
                return "test.PrioritySvc"

            @property
            def proto_file_name(self) -> str:
                return "test.proto"

        conn = RecordingConnection()

        class _Ctx:
            connection = conn
            factory = None

        svc = _Svc(_Ctx(), MessageServiceOptions(max_concurrent=1, max_priority=2))
        await svc._listener.init(handler=lambda *a: None, queue_name=svc.service_name)

        assert declare_for("test.PrioritySvc", conn)["arguments"] == {"x-max-priority": 2}


# ==========================================================================
# 3. Validation
#
# aio-pika silently truncates a float priority (1.5 -> 1) and only fails on an
# out-of-range one deep in the encoder, as a raw struct.error. Both are
# verified below against a real broker. Validating at our own seam turns both
# into one clear error and keeps Python's behaviour identical to the TS port's.
# ==========================================================================
class TestValidation:
    @pytest.mark.parametrize("bad", [0, -1, 256, 300, 1.5, 2.0, "2", True])
    def test_bad_max_priority_is_rejected(self, bad):
        with pytest.raises(InvalidPriorityError):
            BaseListener(
                RecordingConnection(), late_ack=True, max_concurrent=1, max_priority=bad
            )

    @pytest.mark.parametrize("good", [1, 2, 10, 255])
    def test_good_max_priority_is_accepted(self, good):
        listener = BaseListener(
            RecordingConnection(), late_ack=True, max_concurrent=1, max_priority=good
        )
        assert listener._max_priority == good

    def test_none_max_priority_is_accepted_and_means_off(self):
        assert BaseListener(RecordingConnection())._max_priority is None

    async def test_bad_max_priority_never_reaches_the_broker(self):
        """
        The declare is what 406s, and a 406 kills a channel on the shared
        connection. So an invalid value has to fail before anything is sent.
        """
        conn = RecordingConnection()
        with pytest.raises(InvalidPriorityError):
            BaseListener(conn, late_ack=True, max_concurrent=1, max_priority=300)
        assert conn.queue_declares == []

    @pytest.mark.parametrize("bad", [-1, 256, 1.5, 2.0, "2", True])
    async def test_bad_message_priority_is_rejected(self, bad):
        conn = RecordingConnection()
        c = Connection()
        c._is_connected = True
        with pytest.raises(InvalidPriorityError):
            await c.publish(
                channel=None,
                exchange=_ExplodingExchange(),
                routing_key="x",
                body=b"x",
                properties={"priority": bad},
            )

    @pytest.mark.parametrize("good", [0, 1, 2, 255])
    async def test_good_message_priority_is_accepted(self, good):
        c = Connection()
        c._is_connected = True
        ex = _CapturingExchange()
        await c.publish(None, ex, "x", b"x", properties={"priority": good})
        assert ex.messages[0].priority == good


class _CapturingExchange:
    def __init__(self) -> None:
        self.messages: List[Message] = []

    async def publish(self, message, routing_key=None, **kwargs):
        self.messages.append(message)


class _ExplodingExchange:
    async def publish(self, message, routing_key=None, **kwargs):
        raise AssertionError("publish must not be reached for an invalid priority")


# ==========================================================================
# 4. Publish path
# ==========================================================================
class TestPublishPath:
    async def test_publish_without_priority_is_byte_identical_to_before(self):
        """
        The other half of backward compatibility: a caller that does not pass
        a priority must produce the same AMQP frame as before this change.

        Note the expected value is 0, not None. aio-pika normalizes an unset
        priority via ``optional(priority, int, 0)``, so protobus-py has always
        put ``priority: 0`` on the wire — that is unchanged here, not
        introduced here. The broker treats an absent priority and an explicit 0
        identically (pinned by
        test_an_unset_priority_and_zero_sort_identically), so this is also the
        point at which the Python port's frame differs harmlessly from the TS
        port's, where amqplib omits the property entirely.
        """
        c = Connection()
        c._is_connected = True
        ex = _CapturingExchange()
        await c.publish(None, ex, "x", b"x")
        assert ex.messages[0].priority == 0

    async def test_publish_with_priority_sets_the_property(self):
        c = Connection()
        c._is_connected = True
        ex = _CapturingExchange()
        await c.publish(None, ex, "x", b"x", properties={"priority": Config.PRIORITY_CONTROL})
        assert ex.messages[0].priority == 2

    async def test_service_proxy_forwards_priority(self):
        captured: Dict[str, Any] = {}

        class _Factory:
            def build_request(self, method, message, actor):
                return b"body"

            def is_streaming_method(self, name):
                return False

            @property
            def root(self):
                class _R:
                    def lookup_service(self, n):
                        return None

                return _R()

        class _Ctx:
            factory = _Factory()

            async def publish_message(self, data, routing_key, rpc=True, priority=None):
                captured["priority"] = priority
                captured["rpc"] = rpc
                return None

        proxy = ServiceProxy(_Ctx(), "test.Svc")
        await proxy.init()
        await proxy.doThing({}, None, False, priority=Config.PRIORITY_CONTROL)
        assert captured["priority"] == 2

    async def test_service_proxy_priority_defaults_to_none(self):
        captured: Dict[str, Any] = {}

        class _Factory:
            def build_request(self, method, message, actor):
                return b"body"

            def is_streaming_method(self, name):
                return False

            @property
            def root(self):
                class _R:
                    def lookup_service(self, n):
                        return None

                return _R()

        class _Ctx:
            factory = _Factory()

            async def publish_message(self, data, routing_key, rpc=True, priority=None):
                captured["priority"] = priority
                return None

        proxy = ServiceProxy(_Ctx(), "test.Svc")
        await proxy.init()
        await proxy.doThing({}, None, False)
        assert captured["priority"] is None

    async def test_proxy_still_works_with_a_context_predating_the_parameter(self):
        """
        IContext is a Protocol, so third parties supply their own contexts.
        One written before `priority` existed has a 3-argument
        publish_message; the proxy must not start passing a 4th to it just
        because the feature now exists. It only forwards `priority` when a
        caller actually asked for one.
        """
        class _Factory:
            def build_request(self, method, message, actor):
                return b"body"

            def is_streaming_method(self, name):
                return False

            @property
            def root(self):
                class _R:
                    def lookup_service(self, n):
                        return None

                return _R()

        class _LegacyCtx:
            factory = _Factory()

            # No `priority` parameter at all — as it was before this change.
            async def publish_message(self, data, routing_key, rpc=True):
                return None

        proxy = ServiceProxy(_LegacyCtx(), "test.Svc")
        await proxy.init()
        await proxy.doThing({}, None, False)  # must not raise TypeError

        # PRIORITY_NORMAL is documented as identical to passing nothing, and
        # aio-pika normalizes unset to 0 anyway — so forwarding it would take
        # on this compatibility risk for zero behavioural benefit. It must
        # take the same path as omitting it.
        await proxy.doThing({}, None, False, priority=Config.PRIORITY_NORMAL)

        # Asking for a priority that actually means something does fail
        # loudly, rather than silently dropping it on the floor. It surfaces
        # as PublishMessageError because the proxy wraps dispatch failures.
        with pytest.raises(PublishMessageError):
            await proxy.doThing({}, None, False, priority=Config.PRIORITY_CONTROL)

    async def test_service_proxy_priority_is_keyword_only(self):
        """
        Positional-compatible by construction: no existing call site can
        accidentally bind its 4th positional argument to `priority`.
        """
        class _Factory:
            def build_request(self, method, message, actor):
                return b"body"

            def is_streaming_method(self, name):
                return False

            @property
            def root(self):
                class _R:
                    def lookup_service(self, n):
                        return None

                return _R()

        class _Ctx:
            factory = _Factory()

            async def publish_message(self, data, routing_key, rpc=True, priority=None):
                return None

        proxy = ServiceProxy(_Ctx(), "test.Svc")
        await proxy.init()
        with pytest.raises(TypeError):
            await proxy.doThing({}, None, False, 2)


# ==========================================================================
# 4b. Retry must not demote a message
#
# The broker preserves `priority` across a dead-letter hop, but protobus does
# not lean on that for retries: Connection._retry_message RE-PUBLISHES the
# message to <routing_key>.retry. Anything that publish does not copy is lost.
# A control message that fails once and comes back as priority 0 would land
# behind the entire bulk backlog — exactly the failure this feature exists to
# prevent, and invisible unless something fails first.
# ==========================================================================
class _FakeIncoming:
    def __init__(self, priority=None, headers=None):
        self.body = b"payload"
        self.headers = headers or {}
        self.correlation_id = "cid-1"
        self.reply_to = "reply.q"
        self.routing_key = "REQUEST.svc.Thing.control"
        self.priority = priority


class _FakeChannel:
    def __init__(self, exchange):
        self._exchange = exchange
        self.default_exchange = exchange
        self.declared = []

    async def get_exchange(self, name):
        return self._exchange

    async def declare_queue(self, name, durable=False, **kwargs):
        self.declared.append(name)

        class _Q:
            pass

        return _Q()


class TestRetryPreservesPriority:
    async def test_retry_republish_keeps_the_original_priority(self):
        """
        A control message that fails and retries must come back as control
        traffic, not as bulk.
        """
        ex = _CapturingExchange()
        ch = _FakeChannel(ex)
        c = Connection()
        c._is_connected = True

        await c._retry_message(
            ch,
            _FakeIncoming(priority=Config.PRIORITY_CONTROL),
            retry_count=0,
            error=Exception("boom"),
            retry_opts=RetryOptions(max_retries=3, retry_delay_ms=5000),
        )

        assert ex.messages, "nothing was republished to the retry queue"
        assert ex.messages[0].priority == Config.PRIORITY_CONTROL

    async def test_retry_republish_of_an_unprioritised_message_is_unchanged(self):
        """The default path must not acquire a priority it never had."""
        ex = _CapturingExchange()
        ch = _FakeChannel(ex)
        c = Connection()
        c._is_connected = True

        await c._retry_message(
            ch,
            _FakeIncoming(priority=None),
            retry_count=0,
            error=Exception("boom"),
            retry_opts=RetryOptions(max_retries=3, retry_delay_ms=5000),
        )

        # 0 is aio-pika's normalization of "unset" — the same frame as before.
        assert ex.messages[0].priority == 0

    async def test_dlq_republish_keeps_the_original_priority(self):
        """
        The DLQ is a plain queue, so priority buys no ordering there — but
        dropping it discards information about what the message was, which is
        the one thing a DLQ exists to preserve.
        """
        ex = _CapturingExchange()
        ch = _FakeChannel(ex)
        c = Connection()
        c._is_connected = True

        await c._send_to_dlq(
            ch, _FakeIncoming(priority=Config.PRIORITY_CONTROL), Exception("dead")
        )

        assert ex.messages, "nothing was published to the DLQ"
        assert ex.messages[0].priority == Config.PRIORITY_CONTROL


# ==========================================================================
# 5. Against a real broker
# ==========================================================================
@pytest.fixture
async def amqp():
    conn = await aio_pika.connect_robust(RABBITMQ_URL)
    yield conn
    await conn.close()


class TestAgainstRealBroker:
    async def test_priority_queue_delivers_high_priority_first(self, amqp):
        """The whole point of the feature."""
        ch = await amqp.channel()
        name = f"pbtest.prio.{uuid.uuid4().hex[:8]}"
        q = await ch.declare_queue(
            name, durable=False, auto_delete=True, arguments={"x-max-priority": 2}
        )
        for i in range(10):
            await ch.default_exchange.publish(
                Message(body=f"low{i}".encode(), priority=Config.PRIORITY_NORMAL),
                routing_key=name,
            )
        await ch.default_exchange.publish(
            Message(body=b"control", priority=Config.PRIORITY_CONTROL), routing_key=name
        )

        order = [(await q.get(no_ack=True)).body.decode() for _ in range(11)]
        assert order[0] == "control", order

    async def test_priority_on_a_non_priority_queue_is_ignored_not_an_error(self, amqp):
        """
        What lets a NEW publisher talk to an OLD consumer: the broker accepts
        the priority property on a plain queue, ignores it for ordering, and
        leaves the channel open.
        """
        ch = await amqp.channel()
        name = f"pbtest.plain.{uuid.uuid4().hex[:8]}"
        q = await ch.declare_queue(name, durable=False, auto_delete=True)

        await ch.default_exchange.publish(
            Message(body=b"low", priority=Config.PRIORITY_NORMAL), routing_key=name
        )
        await ch.default_exchange.publish(
            Message(body=b"control", priority=Config.PRIORITY_CONTROL), routing_key=name
        )

        first = await q.get(no_ack=True)
        second = await q.get(no_ack=True)
        assert not ch.is_closed
        assert (first.body, second.body) == (b"low", b"control")  # FIFO, unchanged

    async def test_redeclaring_an_existing_queue_with_max_priority_is_a_406(self, amqp):
        """
        The oracle for the migration note in the README. If this ever stops
        throwing, the note is wrong and should be deleted.
        """
        name = f"pbtest.mig.{uuid.uuid4().hex[:8]}"
        ch = await amqp.channel()
        await ch.declare_queue(name, durable=True, auto_delete=False)

        ch2 = await amqp.channel()
        with pytest.raises(aio_pika.exceptions.ChannelPreconditionFailed):
            await ch2.declare_queue(
                name, durable=True, auto_delete=False, arguments={"x-max-priority": 2}
            )
        assert ch2.is_closed  # and it took the channel down with it

        ch3 = await amqp.channel()
        await ch3.queue_delete(name)

    async def test_a_406_does_not_take_down_other_listeners_on_the_connection(self):
        """
        Bounds the migration hazard honestly.

        It is tempting to say "protobus shares one connection, so a 406 is a
        process-wide outage". Measured, that is not true: each listener holds
        its OWN channel, and a 406 closes only that one. The connection stays
        up and unrelated listeners keep consuming.

        What it does cost is real but narrower — the declare happens inside
        init(), so the offending listener never starts and the service fails to
        boot. This test pins both halves so the docs cannot drift back into
        overstating it.
        """
        suffix = uuid.uuid4().hex[:8]
        victim = f"pbtest.blast.victim.{suffix}"
        healthy = f"pbtest.blast.healthy.{suffix}"

        ctx = Context()
        await ctx.init(RABBITMQ_URL)
        raw = await aio_pika.connect_robust(RABBITMQ_URL)
        try:
            # A plain queue that already exists, as a deployed service would have.
            rch = await raw.channel()
            await rch.declare_queue(victim, durable=True, auto_delete=False)

            received: List[str] = []

            async def healthy_handler(body: bytes, correlation_id: str, headers=None):
                received.append(body.decode())
                return None

            good = MessageListener(
                ctx.connection,
                late_ack=True,
                max_concurrent=1,
                retry_options=RetryOptions(max_retries=0),
            )
            await good.init(healthy_handler, healthy)
            await good.subscribe(f"REQUEST.{healthy}.*")
            await good.start()

            # Now opt a DIFFERENT listener into priority against the
            # pre-existing plain queue. This is the migration someone forgot.
            offender = MessageListener(
                ctx.connection,
                late_ack=True,
                max_concurrent=1,
                retry_options=RetryOptions(max_retries=0),
                max_priority=2,
            )
            with pytest.raises(aio_pika.exceptions.ChannelPreconditionFailed):
                await offender.init(lambda *a, **k: None, victim)

            # The blast radius: the connection is fine...
            assert ctx.is_connected
            # ...and the unrelated listener is still consuming.
            await ctx.publish_message(
                b"ping", f"REQUEST.{healthy}.ping", rpc=False
            )
            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.1)
            assert received == ["ping"]

            await good.close()
        finally:
            ch = await raw.channel()
            for n in (victim, healthy):
                try:
                    await ch.queue_delete(n)
                except Exception:
                    ch = await raw.channel()
            await raw.close()
            await ctx.close()

    async def test_redeclaring_with_the_same_max_priority_is_idempotent(self, amqp):
        """Restarting a service that already opted in must not 406."""
        name = f"pbtest.idem.{uuid.uuid4().hex[:8]}"
        ch = await amqp.channel()
        await ch.declare_queue(
            name, durable=True, auto_delete=False, arguments={"x-max-priority": 2}
        )
        ch2 = await amqp.channel()
        await ch2.declare_queue(
            name, durable=True, auto_delete=False, arguments={"x-max-priority": 2}
        )
        assert not ch2.is_closed
        await ch2.queue_delete(name)

    async def test_dead_lettering_preserves_priority(self, amqp):
        """
        Why the .retry queue needs no x-max-priority of its own: a message
        keeps its priority across the dead-letter hop, so it re-sorts when it
        lands back on the main priority queue.
        """
        ch = await amqp.channel()
        suffix = uuid.uuid4().hex[:8]
        target = f"pbtest.dlx.target.{suffix}"
        source = f"pbtest.dlx.source.{suffix}"

        tq = await ch.declare_queue(target, durable=False, auto_delete=False)
        await ch.declare_queue(
            source,
            durable=False,
            auto_delete=False,
            arguments={
                "x-message-ttl": 50,
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": target,
            },
        )
        await ch.default_exchange.publish(
            Message(body=b"retried", priority=Config.PRIORITY_CONTROL), routing_key=source
        )

        got = None
        for _ in range(50):
            got = await tq.get(no_ack=True, fail=False)
            if got is not None:
                break
            await asyncio.sleep(0.1)

        assert got is not None, "message never dead-lettered"
        assert got.priority == Config.PRIORITY_CONTROL

        ch2 = await amqp.channel()
        await ch2.queue_delete(source)
        await ch2.queue_delete(target)

    async def test_an_unset_priority_and_zero_sort_identically(self, amqp):
        """
        The cross-port compatibility oracle.

        amqplib (TS) omits the priority property when it is unset; aio-pika
        (Python) always encodes 0. The two ports therefore put different bytes
        on the wire for "no priority" — this test pins that the BROKER cannot
        tell the difference, which is what makes that divergence harmless.
        """
        ch = await amqp.channel()
        name = f"pbtest.zero.{uuid.uuid4().hex[:8]}"
        q = await ch.declare_queue(
            name, durable=False, auto_delete=True, arguments={"x-max-priority": 2}
        )
        await ch.default_exchange.publish(Message(body=b"unset"), routing_key=name)
        await ch.default_exchange.publish(
            Message(body=b"zero", priority=0), routing_key=name
        )
        await ch.default_exchange.publish(
            Message(body=b"control", priority=2), routing_key=name
        )

        order = [(await q.get(no_ack=True)).body.decode() for _ in range(3)]
        # control jumps both; unset and zero keep their publish order relative
        # to each other, i.e. they are the same priority as far as RabbitMQ is
        # concerned.
        assert order == ["control", "unset", "zero"]

    async def test_a_priority_above_the_ceiling_is_clamped_for_ordering(self, amqp):
        """
        Pins the claim in docs/advanced/message-priority.md that publishing
        above x-max-priority is pointless rather than harmful.

        `at` is published at exactly the ceiling and `over` well above it. If
        the broker honoured 5 as 5, `over` would jump `at`. It does not — they
        sort as equals and stay in publish order — but the property value is
        stored unchanged.
        """
        ch = await amqp.channel()
        name = f"pbtest.ceil.{uuid.uuid4().hex[:8]}"
        q = await ch.declare_queue(
            name, durable=False, auto_delete=True, arguments={"x-max-priority": 2}
        )
        await ch.default_exchange.publish(Message(body=b"at", priority=2), routing_key=name)
        await ch.default_exchange.publish(Message(body=b"over", priority=5), routing_key=name)
        await ch.default_exchange.publish(Message(body=b"low", priority=0), routing_key=name)

        got = [await q.get(no_ack=True) for _ in range(3)]
        assert not ch.is_closed
        assert [m.body.decode() for m in got] == ["at", "over", "low"]
        assert got[1].priority == 5  # clamped for ordering, preserved as data

    async def test_aio_pika_silently_truncates_a_float_priority(self, amqp):
        """
        Pins the reason validation exists. aio-pika does int(priority), so 1.5
        becomes 1 with no error anywhere. If this ever starts raising on its
        own, our validation is belt-and-braces rather than load-bearing — but
        the TS port would still need it, so it stays either way.
        """
        ch = await amqp.channel()
        name = f"pbtest.trunc.{uuid.uuid4().hex[:8]}"
        q = await ch.declare_queue(name, durable=False, auto_delete=True)
        await ch.default_exchange.publish(Message(body=b"x", priority=1.5), routing_key=name)
        got = await q.get(no_ack=True)
        assert got.priority == 1

    async def test_out_of_range_priority_fails_in_the_encoder_not_the_api(self, amqp):
        """
        The other half: 256 is accepted by Message() and only blows up deep in
        pamqp as a raw struct error. Our InvalidPriorityError replaces this.
        """
        ch = await amqp.channel()
        name = f"pbtest.range.{uuid.uuid4().hex[:8]}"
        await ch.declare_queue(name, durable=False, auto_delete=True)
        assert Message(body=b"x", priority=256).priority == 256
        # struct.error specifically — "'B' format requires 0 <= number <= 255",
        # raised while packing the AMQP octet. Asserting the precise type is
        # the point: a blind `Exception` here would also be satisfied by a
        # connection failure, which would make this test pass for the wrong
        # reason and quietly stop justifying the validation.
        with pytest.raises(struct.error):
            await ch.default_exchange.publish(
                Message(body=b"x", priority=256), routing_key=name
            )


# ==========================================================================
# 6. End to end through the library
# ==========================================================================
class TestEndToEnd:
    async def test_proxy_priority_reaches_the_wire(self):
        """
        Full path: ServiceProxy -> Context -> MessageDispatcher -> AMQP.

        Oracle is an independent observer queue bound to the same routing key
        on the real bus exchange, read with a plain aio-pika client. The
        assertion is the AMQP `priority` property as the broker stored it, so
        nothing in protobus can fake it.
        """
        ctx = Context()
        await ctx.init(RABBITMQ_URL)
        observer_conn = await aio_pika.connect_robust(RABBITMQ_URL)
        try:
            ch = await observer_conn.channel()
            ex = await ch.declare_exchange(
                Config.bus_exchange_name(), ExchangeType.TOPIC, durable=True
            )
            obs = await ch.declare_queue(
                f"pbtest.observer.{uuid.uuid4().hex[:8]}", durable=False, auto_delete=True
            )
            await obs.bind(ex, "REQUEST.test.WireSvc.*")

            ctx.factory.parse("", "test.WireSvc")
            proxy = ServiceProxy(ctx, "test.WireSvc")
            await proxy.init()

            await proxy.control({"x": 1}, "actor", False, priority=Config.PRIORITY_CONTROL)
            await proxy.bulk({"x": 2}, "actor", False)

            got = {}
            for _ in range(2):
                m = None
                for _ in range(50):
                    m = await obs.get(no_ack=True, fail=False)
                    if m is not None:
                        break
                    await asyncio.sleep(0.1)
                assert m is not None, "observer never saw the request"
                got[m.routing_key] = m.priority

            assert got["REQUEST.test.WireSvc.control"] == 2
            # 0, not None — aio-pika has always normalized an unset priority
            # to 0. Unchanged by this feature; see
            # test_publish_without_priority_is_byte_identical_to_before.
            assert got["REQUEST.test.WireSvc.bulk"] == 0
        finally:
            await observer_conn.close()
            await ctx.close()

    async def test_control_jumps_a_backlog_while_the_consumer_is_already_draining(self):
        """
        The scenario that actually matters, and the one
        test_a_priority_listener_drains_control_traffic_first does NOT cover.

        That test queues everything before consuming, and the broker pops a
        queue in priority order regardless of prefetch — so it passes even with
        no prefetch bound at all. The real case is a consumer already draining
        when the bulk arrives, where anything already pushed to the client is
        beyond reordering.

        Made deterministic with a gate: prefetch is 1, and the handler blocks,
        so exactly ONE bulk message is in flight and everything else is still
        in the queue when the control message is published. The control
        message must then come out second — behind the one already prefetched,
        ahead of the other 49. That is precisely the guarantee the docs claim:
        priority reorders the queue, not the prefetch buffer.
        """
        ctx = Context()
        await ctx.init(RABBITMQ_URL)
        queue_name = f"pbtest.caseb.{uuid.uuid4().hex[:8]}"
        seen: List[str] = []
        gate = asyncio.Event()
        first_delivered = asyncio.Event()

        async def handler(body: bytes, correlation_id: str, headers=None):
            seen.append(body.decode())
            first_delivered.set()
            await gate.wait()
            return None

        listener = MessageListener(
            ctx.connection,
            late_ack=True,
            max_concurrent=1,
            retry_options=RetryOptions(max_retries=0),
            max_priority=Config.RECOMMENDED_MAX_PRIORITY,
        )
        try:
            await listener.init(handler, queue_name)
            await listener.subscribe(f"REQUEST.{queue_name}.*")
            await listener.start()

            for i in range(50):
                await ctx.publish_message(
                    f"bulk{i}".encode(),
                    f"REQUEST.{queue_name}.bulk",
                    rpc=False,
                    priority=Config.PRIORITY_NORMAL,
                )

            # One message is now in the handler and blocked; the rest are queued.
            await asyncio.wait_for(first_delivered.wait(), timeout=10)

            await ctx.publish_message(
                b"control",
                f"REQUEST.{queue_name}.control",
                rpc=False,
                priority=Config.PRIORITY_CONTROL,
            )
            await asyncio.sleep(0.5)  # let it land in the queue

            gate.set()
            for _ in range(200):
                if len(seen) >= 51:
                    break
                await asyncio.sleep(0.1)

            assert len(seen) == 51, seen
            assert seen[0].startswith("bulk"), seen[:3]   # the prefetched one
            assert seen[1] == "control", seen[:3]         # jumped the other 49
        finally:
            gate.set()
            await listener.close()
            ch = await ctx.connection.open_channel()
            await ch.queue_delete(queue_name)
            await ctx.close()

    @pytest.mark.parametrize("prefetch", [1, 5])
    async def test_prefetch_is_exactly_the_width_of_the_priority_blind_spot(
        self, prefetch
    ):
        """
        Pins the docs' central caveat as an equality, not a hand-wave.

        The control message comes out at index == prefetch: the N messages
        already pushed to this consumer are past reordering, and the control
        message jumps everything still in the queue behind them. Measured
        across a wider sweep (50 bulk, one replica, only prefetch varying):

            max_concurrent=1   -> CONTROL at index 1
            max_concurrent=5   -> CONTROL at index 5
            max_concurrent=20  -> CONTROL at index 20
            max_concurrent=100 -> CONTROL at index 50   (all 50 prefetched)

        The last row is the point worth internalising: once the prefetch
        exceeds the backlog, priority is completely inert. max_concurrent is
        therefore NOT an independent throughput dial once priority is in play
        — it IS the width of the window priority cannot see into.

        PRECONDITION, asserted below rather than assumed: the consumer must be
        SATURATED — every prefetch slot occupied by an in-flight handler. The
        gate here holds EVERY delivery, not just the first, which is what makes
        that true. It matters because protobus does not serialise deliveries: a
        free prefetch slot keeps pulling from the queue while other handlers
        run, so with a fast handler the backlog drains itself and the control
        message has nothing left to jump. Measured, same setup but holding only
        the first delivery: prefetch 5 and 20 both consumed all 50 bulk
        messages before the control message was even published, putting it at
        index 50. That is not a counter-example — it is measuring the drain
        rate rather than the prefetch window. Hence the peak-in-flight
        assertion: if this test ever stops saturating, it must fail loudly
        rather than quietly measure something else.
        """
        bulk = 30
        ctx = Context()
        await ctx.init(RABBITMQ_URL)
        queue_name = f"pbtest.window.{uuid.uuid4().hex[:8]}"
        seen: List[str] = []
        gate = asyncio.Event()
        first_delivered = asyncio.Event()
        in_flight = 0
        peak_in_flight = 0

        async def handler(body: bytes, correlation_id: str, headers=None):
            nonlocal in_flight, peak_in_flight
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
            seen.append(body.decode())
            first_delivered.set()
            await gate.wait()   # holds EVERY delivery — see PRECONDITION above
            in_flight -= 1
            return None

        listener = MessageListener(
            ctx.connection,
            late_ack=True,
            max_concurrent=prefetch,
            retry_options=RetryOptions(max_retries=0),
            max_priority=Config.RECOMMENDED_MAX_PRIORITY,
        )
        try:
            await listener.init(handler, queue_name)
            await listener.subscribe(f"REQUEST.{queue_name}.*")
            await listener.start()

            for i in range(bulk):
                await ctx.publish_message(
                    f"bulk{i}".encode(),
                    f"REQUEST.{queue_name}.bulk",
                    rpc=False,
                    priority=Config.PRIORITY_NORMAL,
                )

            await asyncio.wait_for(first_delivered.wait(), timeout=10)
            await asyncio.sleep(1.0)  # let the broker fill the prefetch window

            # The precondition, asserted before the thing it makes true: every
            # prefetch slot must be occupied. Without this the test could still
            # go green while measuring how fast the backlog drained itself.
            assert peak_in_flight == prefetch, (
                f"consumer not saturated: peak in-flight {peak_in_flight} != "
                f"prefetch {prefetch}; this test would be measuring the drain "
                f"rate, not the prefetch window"
            )
            assert len(seen) == prefetch, (
                f"expected exactly {prefetch} bulk messages held in flight, "
                f"got {len(seen)}"
            )

            await ctx.publish_message(
                b"control",
                f"REQUEST.{queue_name}.control",
                rpc=False,
                priority=Config.PRIORITY_CONTROL,
            )
            await asyncio.sleep(1.0)

            gate.set()
            for _ in range(200):
                if len(seen) >= bulk + 1:
                    break
                await asyncio.sleep(0.1)

            assert len(seen) == bulk + 1, seen
            assert seen.index("control") == prefetch, (
                f"prefetch={prefetch} but control landed at "
                f"{seen.index('control')}: {seen[: prefetch + 3]}"
            )
        finally:
            gate.set()
            await listener.close()
            ch = await ctx.connection.open_channel()
            await ch.queue_delete(queue_name)
            await ctx.close()

    async def test_a_priority_listener_drains_control_traffic_first(self):
        """
        The ONIT case, reduced: a backlog of bulk work already queued, then one
        control message. All messages are enqueued before consumption starts,
        so nothing is prefetched and the ordering is deterministic.
        """
        ctx = Context()
        await ctx.init(RABBITMQ_URL)
        queue_name = f"pbtest.svc.{uuid.uuid4().hex[:8]}"
        seen: List[str] = []

        listener = MessageListener(
            ctx.connection,
            late_ack=True,
            max_concurrent=1,
            retry_options=RetryOptions(max_retries=0),
            max_priority=Config.RECOMMENDED_MAX_PRIORITY,
        )

        async def handler(body: bytes, correlation_id: str, headers=None):
            seen.append(body.decode())
            return None

        try:
            await listener.init(handler, queue_name)
            await listener.subscribe(f"REQUEST.{queue_name}.*")

            for i in range(20):
                await ctx._message_dispatcher.publish(
                    f"bulk{i}".encode(),
                    f"REQUEST.{queue_name}.bulk",
                    rpc=False,
                    priority=Config.PRIORITY_NORMAL,
                )
            await ctx._message_dispatcher.publish(
                b"control",
                f"REQUEST.{queue_name}.control",
                rpc=False,
                priority=Config.PRIORITY_CONTROL,
            )
            await asyncio.sleep(1.0)  # let everything land before consuming

            await listener.start()
            for _ in range(100):
                if len(seen) >= 21:
                    break
                await asyncio.sleep(0.1)

            assert len(seen) == 21, seen
            assert seen[0] == "control", seen[:5]
        finally:
            await listener.close()
            ch = await ctx.connection.open_channel()
            await ch.queue_delete(queue_name)
            await ctx.close()
