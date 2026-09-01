"""
A request must be dispatched against the receiving service's own contract.

Parity with TS protobus 4085332 ("bind request dispatch to the service's own
contract"), 908d5c8 ("dispatch ignored the broker routing key, so the message
body chose the method") and 6c9b12d ("answer a message that cannot be
understood instead of retrying it").

The Python port resolved the handler as ``getattr(self, method.split('.')[-1])``
with no check against the .proto, the service's own name, or the routing key
RabbitMQ actually authorised. ``getattr`` walks the whole MRO, so a publisher
holding nothing more than the ordinary right to call the service could reach
``init``, ``publish_event``, ``cleanup`` or ``_on_message`` by naming them.
"""

import asyncio
import os

import pytest

from protobus.errors import InvalidMethodError, ProtocolError
from protobus.message_factory import MessageFactory
from protobus.message_service import MessageService

PROTO_DIR = os.path.join(os.path.dirname(__file__), "streaming_proto")


class _FakeConnection:
    """Enough of IConnection for MessageService construction."""

    def __init__(self):
        self.handlers = {}

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def off(self, event, callback):
        self.handlers.get(event, []).remove(callback)


class _FakeContext:
    def __init__(self, factory):
        self._factory = factory
        self._connection = _FakeConnection()

    @property
    def factory(self):
        return self._factory

    @property
    def connection(self):
        return self._connection


class GuardedService(MessageService):
    """`shoot` is the only method this service declares to the world."""

    def __init__(self, context):
        super().__init__(context)
        self.reached = []

    @property
    def service_name(self) -> str:
        return "audit.Guarded"

    @property
    def proto_file_name(self) -> str:
        return "audit.proto"

    async def shoot(self, data, actor, correlation_id):
        self.reached.append("shoot")
        return {"hit": True}

    # A public member that is NOT part of the service contract. Nothing on the
    # bus should be able to reach it, but it is the kind of helper services
    # routinely define next to their handlers.
    async def rotate_credentials(self, *args, **kwargs):
        self.reached.append("rotate_credentials")
        return {"rotated": True}


class CounterService(MessageService):
    """Backed by streaming_test.proto, which declares only `add` and `tick`."""

    def __init__(self, context):
        super().__init__(context)
        self.reached = []

    @property
    def service_name(self) -> str:
        return "streaming_test.Counter"

    @property
    def proto_file_name(self) -> str:
        return "streaming_test.proto"

    async def add(self, data, actor, correlation_id):
        self.reached.append("add")
        return {"sum": data.get("a", 0) + data.get("b", 0)}

    async def rotate_credentials(self, *args, **kwargs):
        self.reached.append("rotate_credentials")
        return {"rotated": True}


@pytest.fixture
async def service():
    factory = MessageFactory()
    await factory.init()
    ctx = _FakeContext(factory)
    svc = GuardedService(ctx)
    factory.parse("", svc.service_name)
    return svc


def _request(factory, method, data=None):
    return factory.build_request(method, data or {}, "actor-1")


def _decode(factory, raw):
    return factory.decode_response(raw)


class TestTheContractIsHonoured:
    async def test_a_declared_method_is_dispatched(self, service):
        factory = service._context.factory
        raw = await service._on_message(
            _request(factory, "audit.Guarded.shoot"),
            "corr-1",
            {},
            routing_key="REQUEST.audit.Guarded.shoot",
        )
        response = _decode(factory, raw)
        assert service.reached == ["shoot"]
        assert response.error is None


class TestMembersOutsideTheContractAreUnreachable:
    @pytest.mark.parametrize(
        "framework_member",
        ["init", "publish_event", "subscribe_event", "_on_message", "_stream_responses"],
    )
    async def test_a_framework_member_cannot_be_called(self, service, framework_member):
        """`getattr(self, name)` walked the MRO and found every one of these."""
        factory = service._context.factory
        method = f"audit.Guarded.{framework_member}"
        raw = await service._on_message(
            _request(factory, method), "corr-1", {}, routing_key=f"REQUEST.{method}"
        )
        response = _decode(factory, raw)
        assert response.error is not None, (
            f"{framework_member} was dispatched instead of being refused"
        )
        assert service.reached == []

    async def test_an_undeclared_public_helper_is_reachable_without_a_proto(
        self, service
    ):
        """Documents the limit of the guard rather than overstating it.

        With no .proto loaded there is nothing to check a name against, and in
        this port every public ``async def`` on the service IS its RPC surface.
        The guard below covers the case that can be checked."""
        factory = service._context.factory
        method = "audit.Guarded.rotate_credentials"
        raw = await service._on_message(
            _request(factory, method), "corr-1", {}, routing_key=f"REQUEST.{method}"
        )
        assert _decode(factory, raw).error is None
        assert service.reached == ["rotate_credentials"]

    async def test_an_undeclared_public_helper_is_refused_when_a_proto_is_loaded(self):
        """The .proto is the contract. A method it does not declare is not one
        of this service's methods, however the subclass spells it."""
        factory = MessageFactory()
        await factory.init(root_paths=[PROTO_DIR])
        ctx = _FakeContext(factory)
        svc = CounterService(ctx)

        method = "streaming_test.Counter.rotate_credentials"
        raw = await svc._on_message(
            factory.build_request(method, {}, "actor-1"),
            "corr-1",
            {},
            routing_key=f"REQUEST.{method}",
        )
        response = factory.decode_response(raw)
        assert response.error is not None
        assert svc.reached == []

    async def test_a_declared_method_still_dispatches_when_a_proto_is_loaded(self):
        factory = MessageFactory()
        await factory.init(root_paths=[PROTO_DIR])
        ctx = _FakeContext(factory)
        svc = CounterService(ctx)

        method = "streaming_test.Counter.add"
        raw = await svc._on_message(
            factory.build_request(method, {"a": 2, "b": 3}, "actor-1"),
            "corr-1",
            {},
            routing_key=f"REQUEST.{method}",
        )
        response = factory.decode_response(raw)
        assert response.error is None
        assert svc.reached == ["add"]

    async def test_an_appended_segment_does_not_redirect_dispatch(self, service):
        """`audit.Guarded.shoot.publish_event` read the payload as `shoot` and
        delivered the call to the framework's `publish_event`."""
        factory = service._context.factory
        raw = await service._on_message(
            _request(factory, "audit.Guarded.shoot.publish_event"),
            "corr-1",
            {},
            routing_key="REQUEST.audit.Guarded.shoot",
        )
        response = _decode(factory, raw)
        assert response.error is not None
        assert service.reached == []

    async def test_a_foreign_service_name_is_refused(self, service):
        """Naming another service's method had its payload parsed under a
        foreign schema and handed to this service's handler."""
        factory = service._context.factory
        raw = await service._on_message(
            _request(factory, "other.Service.shoot"),
            "corr-1",
            {},
            routing_key="REQUEST.audit.Guarded.shoot",
        )
        response = _decode(factory, raw)
        assert response.error is not None
        assert service.reached == []


class TestTheRoutingKeyIsEnforced:
    async def test_a_body_method_disagreeing_with_the_routing_key_is_refused(
        self, service
    ):
        """RabbitMQ topic permissions are granted per routing key. If the body
        may name a different method, they are unenforceable."""
        factory = service._context.factory
        raw = await service._on_message(
            _request(factory, "audit.Guarded.shoot"),
            "corr-1",
            {},
            routing_key="REQUEST.audit.Guarded.readOnly",
        )
        response = _decode(factory, raw)
        assert response.error is not None
        assert service.reached == []

    async def test_dispatch_still_works_when_no_routing_key_is_supplied(self, service):
        """BaseListener.init() accepts caller-supplied handlers; a connection
        that does not pass a routing key must not break dispatch."""
        factory = service._context.factory
        raw = await service._on_message(
            _request(factory, "audit.Guarded.shoot"), "corr-1", {}
        )
        response = _decode(factory, raw)
        assert response.error is None
        assert service.reached == ["shoot"]


class TestAnUndecodableBodyIsAnsweredNotRetried:
    async def test_garbage_body_produces_an_error_reply(self, service):
        """`decode_request` ran outside any try, so a body that did not parse
        threw a plain Exception: three redeliveries through the retry exchange
        and a DLQ entry, while the caller waited out its RPC timeout for a
        reply the retries were never going to produce."""
        factory = service._context.factory
        raw = await service._on_message(
            b"\xff\xff\xff\xff not protobuf",
            "corr-1",
            {},
            routing_key="REQUEST.audit.Guarded.shoot",
        )
        assert raw is not None, "no reply was produced for an undecodable body"
        response = _decode(factory, raw)
        assert response.error is not None
        assert service.reached == []

    async def test_the_error_reply_does_not_quote_the_payload(self, service):
        """A payload that failed to decode is still a payload."""
        secret = b"\xff\xffhunter2-super-secret"
        raw = await service._on_message(
            secret, "corr-1", {}, routing_key="REQUEST.audit.Guarded.shoot"
        )
        assert b"hunter2" not in raw


class TestErrorRepliesSurviveDecoding:
    def test_an_error_with_no_method_is_still_read_as_an_error(self):
        """`decode_response` required a non-empty `error.method`, so an error
        raised before the method was known decoded as a successful empty
        result — a failure presented to the caller as a success."""

        async def build():
            factory = MessageFactory()
            await factory.init()
            return factory

        factory = asyncio.run(build())
        raw = factory.build_response("", ProtocolError("request did not decode"))
        response = factory.decode_response(raw)
        assert response.error is not None, "an error response decoded as a success"
        assert "did not decode" in response.error["message"]


class TestErrorTypesAreExported:
    def test_protocol_error_is_a_handled_error(self):
        from protobus.errors import HandledError, is_handled_error

        assert issubclass(ProtocolError, HandledError)
        assert is_handled_error(ProtocolError("x"))

    def test_invalid_method_error_is_a_handled_error(self):
        from protobus.errors import HandledError

        assert issubclass(InvalidMethodError, HandledError)
