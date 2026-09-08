"""Settlement ordering, cancel-listener compatibility, the public surface,
and CLI name safety."""

import asyncio
import os

import pytest

import protobus
from protobus import (
    AbortController,
    CancelListener,
    Config,
    Connection,
    EventHandler,
    LogLevel,
    MessageDispatcher,
    MessageHandlerContext,
    MissingProto,
    StreamOptions,
    get_log_level,
    set_log_level,
    set_logger,
)
from protobus.cli.generate_service import InvalidServiceNameError, assert_safe_service_name
from protobus.connection import ConsumeOptions
from protobus.logger import DefaultLogger

from ..helpers import FakeChannel, FakeConnection, make_delivery, tick


class OrderingChannel(FakeChannel):
    """Records ack/publish interleaving, which separate lists cannot express."""

    def __init__(self):
        super().__init__()
        self.events = []

    async def basic_ack(self, delivery_tag, **kw):
        self.events.append("ack")
        await super().basic_ack(delivery_tag, **kw)

    async def basic_reject(self, delivery_tag, requeue=True, **kw):
        self.events.append("reject")
        await super().basic_reject(delivery_tag, requeue=requeue, **kw)

    def basic_publish(self, body, *, exchange="", routing_key="", **kw):
        self.events.append(f"publish:{routing_key}")
        return super().basic_publish(body, exchange=exchange, routing_key=routing_key, **kw)


class TestRequestSettlementOrdering:
    async def test_publishes_the_reply_before_acknowledging_the_request(self):
        conn = Connection()
        ch = OrderingChannel()

        async def handler(*_a):
            return b"the-reply"

        await conn.consume(ch, "Svc.Queue", handler, ConsumeOptions(), True)
        await ch.deliver(make_delivery())
        assert ch.events == ["publish:callback.queue", "ack"]

    async def test_publishes_every_streaming_chunk_before_acknowledging_the_request(self):
        conn = Connection()
        ch = OrderingChannel()

        async def handler(*_a):
            async def chunks():
                yield b"one"
                yield b"two"

            return chunks()

        await conn.consume(ch, "Svc.Queue", handler, ConsumeOptions(), True)
        await ch.deliver(make_delivery())
        assert ch.events[-1] == "ack"
        assert len([e for e in ch.events if e.startswith("publish:")]) == 2

    async def test_does_not_acknowledge_the_request_when_the_reply_publish_fails(self):
        conn = Connection()
        ch = OrderingChannel()

        def broken(*_a, **_kw):
            raise RuntimeError("broker went away")

        ch.basic_publish = broken  # type: ignore[method-assign]

        async def handler(*_a):
            return b"r"

        await conn.consume(ch, "Svc.Queue", handler, ConsumeOptions(), True)
        await ch.deliver(make_delivery())
        # An unsent reply leaves the request unsettled so it is redelivered.
        assert "ack" not in ch.events


class TestCancellationIsAdditive:
    @pytest.fixture(autouse=True)
    def _logger(self):
        level = get_log_level()
        yield
        set_logger(DefaultLogger())
        set_log_level(level)

    async def test_works_against_a_connection_that_does_not_implement_cancel_stream(self):
        conn = FakeConnection()
        conn.cancel_stream = None  # type: ignore[assignment]
        listener = CancelListener(conn)
        await listener.start()
        # Delivering a cancel must not raise when the hook is absent.
        await conn.channels[0].deliver(make_delivery(correlation_id="c"))
        await listener.close()

    async def test_delivers_a_cancel_to_the_connection(self):
        conn = FakeConnection()
        listener = CancelListener(conn)
        await listener.start()
        assert any(e["exchange"] == Config.cancel_exchange_name() and e["type"] == "fanout" for e in conn.declared_exchanges)
        assert conn.declared_queues[-1]["options"]["exclusive"] is True
        assert conn.channels[0].last_consume["no_ack"] is True
        await conn.channels[0].deliver(make_delivery(correlation_id="stream-1"))
        assert conn.cancelled_streams == ["stream-1"]
        await listener.close()
        assert conn.channels[0].closed

    async def test_keeps_the_service_usable_when_the_cancel_exchange_cannot_be_declared(self):
        lines = []

        class Capturing:
            def info(self, m):
                lines.append(str(m))

            warn = debug = error = info

        set_logger(Capturing())
        set_log_level(LogLevel.Info)
        conn = FakeConnection()

        async def refused(*_a, **_kw):
            raise RuntimeError("ACCESS_REFUSED - configure access to exchange")

        conn.declare_exchange = refused  # type: ignore[method-assign]
        listener = CancelListener(conn)
        await listener.start()
        assert any("cancellation unavailable" in line for line in lines)

    async def test_is_restored_after_reconnection(self):
        conn = FakeConnection()
        listener = CancelListener(conn)
        await listener.start()
        assert len(conn.restorers) == 1
        await conn.run_restorers()
        assert len(conn.channels) == 2
        await listener.close()
        assert conn.restorers == []

    async def test_publishes_a_cancel_to_the_fanout_exchange_when_a_stream_is_abandoned(self):
        conn = FakeConnection()
        d = MessageDispatcher(conn)
        await d.init()
        reply = d.publish_streaming(b"req", "REQUEST.A.B.c", 5000)
        await tick(2)  # the request has gone out
        await reply.aclose()
        await tick(2)
        cancels = [p for p in conn.publishes if p["exchange"] == Config.cancel_exchange_name()]
        assert len(cancels) == 1
        assert cancels[0]["routing_key"] == ""
        assert isinstance(cancels[0]["properties"]["correlation_id"], str)

    async def test_cancels_immediately_when_an_abort_signal_fires(self):
        conn = FakeConnection()
        d = MessageDispatcher(conn)
        await d.init()
        controller = AbortController()
        d.publish_streaming(b"req", "REQUEST.A.B.c", 5000, StreamOptions(signal=controller.signal))
        await tick(2)
        assert [p for p in conn.publishes if p["exchange"] == Config.cancel_exchange_name()] == []
        controller.abort()
        await tick(2)
        assert len([p for p in conn.publishes if p["exchange"] == Config.cancel_exchange_name()]) == 1

    async def test_sends_exactly_one_cancel_however_many_times_it_is_triggered(self):
        conn = FakeConnection()
        d = MessageDispatcher(conn)
        await d.init()
        controller = AbortController()
        reply = d.publish_streaming(b"req", "REQUEST.A.B.c", 5000, StreamOptions(signal=controller.signal))
        await tick(2)  # the request has gone out
        controller.abort()
        await reply.aclose()
        await tick(2)
        assert len([p for p in conn.publishes if p["exchange"] == Config.cancel_exchange_name()]) == 1


class TestADrainedListenerStaysDrainedAcrossAReconnection:
    async def test_does_not_resume_consuming_after_stop_consuming(self):
        from protobus import MessageListener

        conn = FakeConnection()
        listener = MessageListener(conn, True, 1)

        async def handler(*_a):
            return None

        await listener.init(handler, "Svc")
        await listener.start()
        assert len(conn.consumes) == 1
        await listener.stop_consuming()
        await conn.run_restorers()
        assert len(conn.consumes) == 1


class TestPublicSurface:
    def test_exports_the_types_that_appear_in_public_signatures(self):
        context = MessageHandlerContext(signal=AbortController().signal, routing_key="REQUEST.Some.Service.method", message_id="abc", redelivered=False)
        assert context.routing_key == "REQUEST.Some.Service.method"
        assert context.redelivered is False
        assert EventHandler is not None
        error = MissingProto("missing_proto_source")
        assert isinstance(error, Exception)

    def test_every_name_in_all_is_importable(self):
        for name in protobus.__all__:
            assert getattr(protobus, name, None) is not None, name

    def test_version_is_2(self):
        assert protobus.__version__.startswith("2.")

    def test_error_classes_expose_codes_where_the_typescript_port_does(self):
        from protobus import (
            ChannelClosedError, InternalServiceError, NotReadyError, ProtocolError,
            PublishConfirmTimeoutError, PublishNackedError, RpcTimeoutError, UnroutableError,
        )

        assert ProtocolError("x").code == "PROTOCOL_ERROR"
        assert InternalServiceError().code == "INTERNAL_ERROR"
        assert NotReadyError("x").code == "NOT_READY"
        assert RpcTimeoutError("x").code == "RPC_TIMEOUT"
        assert PublishNackedError("x").code == "PUBLISH_NACKED"
        assert UnroutableError("x").code == "UNROUTABLE"
        assert PublishConfirmTimeoutError("x").code == "PUBLISH_CONFIRM_TIMEOUT"
        assert ChannelClosedError("x").code == "CHANNEL_CLOSED"

    def test_legacy_names_still_import(self):
        from protobus import MissingProtoError, PublishError, PublishMessageError, UnroutableError

        assert MissingProtoError is MissingProto
        assert PublishMessageError is PublishError
        assert isinstance(UnroutableError("x"), PublishMessageError)


class TestServiceNameValidation:
    @pytest.mark.parametrize("name", ["../escape", "../../etc/passwd", "foo/bar", "foo\\bar", "/absolute", ".", "..", "", "with space", "nul\0byte"])
    def test_rejects(self, name):
        with pytest.raises(InvalidServiceNameError):
            assert_safe_service_name(name)

    @pytest.mark.parametrize("name", ["Calculator", "OrderService", "billing_v2", "Report-Generator", "Svc123"])
    def test_accepts(self, name):
        assert assert_safe_service_name(name) == name

    def test_keeps_generated_paths_inside_the_output_directory(self):
        services_dir = "/tmp/project/src/services"
        name = assert_safe_service_name("Calculator")
        resolved = os.path.realpath(os.path.join(services_dir, name.lower()))
        assert resolved.startswith(os.path.realpath(services_dir) + os.sep)
