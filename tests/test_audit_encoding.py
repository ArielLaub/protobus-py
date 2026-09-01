"""
A payload that cannot be encoded or decoded must say so.

Parity with TS protobus 1e829ad / 908d5c8, where an encode failure is an
``InvalidRequestError`` rather than something quietly reshaped onto the wire.

The Python port had two silent ladders. ``_decode_inner_data`` tried protobuf,
then JSON, then returned ``{}`` — so a request the service could not read was
handed to the handler as though the caller had sent nothing. ``_encode_inner_data``
tried protobuf and then fell back to ``json.dumps``, putting JSON bytes into a
field the peer will decode against a declared protobuf type.
"""

import os

import pytest

from protobus.errors import InvalidRequestError, ProtocolError
from protobus.message_factory import MessageFactory

PROTO_DIR = os.path.join(os.path.dirname(__file__), "streaming_proto")


@pytest.fixture
async def typed_factory():
    """A factory that knows streaming_test.Counter's request/response types."""
    factory = MessageFactory()
    await factory.init(root_paths=[PROTO_DIR])
    return factory


@pytest.fixture
async def bare_factory():
    """No protos loaded — the JSON path is this factory's normal mode."""
    factory = MessageFactory()
    await factory.init()
    return factory


class TestUndecodableDataIsReported:
    def test_decode_raises_instead_of_returning_an_empty_dict(self, typed_factory):
        undecodable = b"\xff\xff\xff\xff"
        with pytest.raises(ProtocolError):
            typed_factory._decode_inner_data(
                undecodable, "streaming_test.AddRequest"
            )

    def test_decode_with_no_declared_type_still_reports_garbage(self, bare_factory):
        with pytest.raises(ProtocolError):
            bare_factory._decode_inner_data(b"\xff\xfe\xfd not json")

    def test_empty_data_is_still_an_empty_dict(self, typed_factory):
        """Absent data is a legitimate empty request, not a failure."""
        assert typed_factory._decode_inner_data(b"", "streaming_test.AddRequest") == {}


class TestUnencodableDataIsReported:
    def test_a_value_of_the_wrong_type_raises(self, typed_factory):
        """`a` is an int32. A string that is not a number cannot be encoded,
        and JSON bytes in an AddRequest field are garbage to the peer."""
        with pytest.raises(InvalidRequestError):
            typed_factory._encode_inner_data(
                {"a": "not-a-number"}, "streaming_test.AddRequest"
            )

    def test_build_request_surfaces_the_encode_failure(self, typed_factory):
        with pytest.raises(InvalidRequestError):
            typed_factory.build_request(
                "streaming_test.Counter.add", {"a": "not-a-number"}
            )


class TestPathsThatMustKeepWorking:
    def test_a_valid_typed_payload_round_trips(self, typed_factory):
        raw = typed_factory._encode_inner_data(
            {"a": 2, "b": 3}, "streaming_test.AddRequest"
        )
        assert typed_factory._decode_inner_data(raw, "streaming_test.AddRequest") == {
            "a": 2,
            "b": 3,
        }

    def test_an_unknown_field_is_dropped_rather_than_raising(self, typed_factory):
        """protobufjs ignores unknown fields on the TS side; matching that keeps
        existing callers working instead of turning a stale key into an outage."""
        raw = typed_factory._encode_inner_data(
            {"a": 2, "b": 3, "legacy_key": "ignored"}, "streaming_test.AddRequest"
        )
        assert typed_factory._decode_inner_data(raw, "streaming_test.AddRequest") == {
            "a": 2,
            "b": 3,
        }

    def test_the_json_path_still_works_when_no_type_is_known(self, bare_factory):
        raw = bare_factory._encode_inner_data({"anything": [1, 2, 3]})
        assert bare_factory._decode_inner_data(raw) == {"anything": [1, 2, 3]}

    def test_request_round_trip_without_protos(self, bare_factory):
        raw = bare_factory.build_request("some.Service.method", {"x": 1}, "actor")
        decoded = bare_factory.decode_request(raw)
        assert decoded.method == "some.Service.method"
        assert decoded.data == {"x": 1}
        assert decoded.actor == "actor"


class TestProtoRegistrationFailuresAreNotSwallowed:
    async def test_only_a_missing_proto_is_tolerated_at_startup(self):
        """`RunnableService.start` wrapped schema registration in a bare
        `except Exception` and fell back to registering an empty schema, so any
        failure — not just the intended "this service has no .proto" — produced
        a service that came up looking healthy and failed every call."""
        from protobus.runnable_service import RunnableService

        boom = RuntimeError("disk on fire")

        class ExplodingProtoService(RunnableService):
            @property
            def service_name(self) -> str:
                return "audit.Exploding"

            @property
            def Proto(self) -> str:
                raise boom

        factory = MessageFactory()
        await factory.init()
        ctx = _FakeContext(factory)

        with pytest.raises(RuntimeError, match="disk on fire"):
            await ExplodingProtoService.start(ctx, ExplodingProtoService)

    async def test_a_service_with_no_proto_file_still_starts(self):
        """The tolerated case: JSON-mode services legitimately have no .proto."""
        from protobus.errors import MissingProtoError
        from protobus.runnable_service import RunnableService

        class NoProtoService(RunnableService):
            @property
            def service_name(self) -> str:
                return "audit.NoProto"

            @property
            def Proto(self) -> str:
                raise MissingProtoError("missing_proto_source")

        factory = MessageFactory()
        await factory.init()
        ctx = _FakeContext(factory)
        svc = NoProtoService(ctx)
        # Registration alone must not raise for a missing proto.
        NoProtoService._register_schema(ctx, svc)
        assert factory._proto_sources["audit.NoProto"] == ""


class _FakeConnection:
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
