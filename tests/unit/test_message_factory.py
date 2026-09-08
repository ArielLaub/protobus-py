"""MessageFactory: schema loading, proto3 defaults, registration, packages."""

import os
import tempfile

import pytest

from protobus import (
    HandledError,
    InvalidMethodError,
    InvalidMethodNameError,
    MessageFactory,
    MessageService,
    NotInitializedError,
    ProtoParseError,
    UnknownMethodError,
    UnknownTypeError,
    is_handled_error,
)
from protobus.connection import AbortController, MessageHandlerContext
from protobus.message_factory import _RequestContainerMsg

from ..helpers import FakeConnection, FakeContext, make_factory

DEFAULTS_PROTO = """syntax = "proto3";
package T;
message Turn {
    int32 next_index = 1;
    string label = 2;
    bool active = 3;
    double ratio = 4;
    repeated int32 tags = 5;
    map<string, int32> scores = 6;
    Res nested = 7;
    optional int32 maybe = 8;
    oneof choice { string a = 9; int32 b = 10; }
    Kind kind = 11;
    bytes blob = 12;
    int64 big = 13;
    uint64 ubig = 14;
}
enum Kind { NONE = 0; FIRE = 1; }
message Res { bool ok = 1; }
service Api { rpc play (T.Turn) returns (T.Res); }"""


def round_trip(f, obj):
    return f.decode_request(f.build_request("T.Api.play", obj, "tester")).data


class TestProto3DefaultValuesSurviveARoundTrip:
    def test_keeps_a_zero_integer_as_0_not_missing(self):
        assert round_trip(make_factory(DEFAULTS_PROTO), {"next_index": 0, "label": "x"})["next_index"] == 0

    def test_keeps_non_zero_integers_intact(self):
        assert round_trip(make_factory(DEFAULTS_PROTO), {"next_index": 4})["next_index"] == 4

    def test_keeps_an_empty_string_and_a_false_boolean(self):
        out = round_trip(make_factory(DEFAULTS_PROTO), {"next_index": 1, "label": "", "active": False})
        assert out["label"] == ""
        assert out["active"] is False

    def test_keeps_a_zero_double(self):
        assert round_trip(make_factory(DEFAULTS_PROTO), {"ratio": 0})["ratio"] == 0.0

    def test_gives_an_unset_repeated_field_an_empty_list_and_a_map_an_empty_dict(self):
        out = round_trip(make_factory(DEFAULTS_PROTO), {"next_index": 1})
        assert out["tags"] == []
        assert out["scores"] == {}

    def test_supplies_defaults_for_fields_the_sender_omitted_entirely(self):
        out = round_trip(make_factory(DEFAULTS_PROTO), {"label": "only this"})
        assert out["next_index"] == 0
        assert out["active"] is False
        assert out["kind"] == "NONE"
        assert out["blob"] == b""
        assert out["big"] == 0

    def test_an_unset_message_field_is_none_and_a_set_one_is_a_dict(self):
        f = make_factory(DEFAULTS_PROTO)
        assert round_trip(f, {})["nested"] is None
        assert round_trip(f, {"nested": {"ok": True}})["nested"] == {"ok": True}
        assert round_trip(f, {"nested": {}})["nested"] == {"ok": False}

    def test_presence_is_kept_for_optional_and_oneof_fields(self):
        f = make_factory(DEFAULTS_PROTO)
        assert "maybe" not in round_trip(f, {})
        assert round_trip(f, {"maybe": 0})["maybe"] == 0
        out = round_trip(f, {"b": 0})
        assert out["b"] == 0 and "a" not in out

    def test_enums_decode_to_their_names_and_accept_names_or_numbers(self):
        f = make_factory(DEFAULTS_PROTO)
        assert round_trip(f, {"kind": "FIRE"})["kind"] == "FIRE"
        assert round_trip(f, {"kind": 1})["kind"] == "FIRE"
        with pytest.raises(ValueError):
            round_trip(f, {"kind": "WATER"})

    def test_64_bit_integers_are_native_ints(self):
        f = make_factory(DEFAULTS_PROTO)
        out = round_trip(f, {"big": -(2**62), "ubig": 2**64 - 1})
        assert out["big"] == -(2**62)
        assert out["ubig"] == 2**64 - 1
        # Strings are accepted on input, as the TypeScript port accepts them.
        assert round_trip(f, {"big": "9007199254740993"})["big"] == 9007199254740993

    def test_bytes_are_bytes(self):
        f = make_factory(DEFAULTS_PROTO)
        assert round_trip(f, {"blob": b"\x00\xff"})["blob"] == b"\x00\xff"
        assert round_trip(f, {"blob": bytearray(b"ab")})["blob"] == b"ab"

    def test_unknown_keys_are_ignored(self):
        assert "nope" not in round_trip(make_factory(DEFAULTS_PROTO), {"nope": 1, "label": "x"})

    def test_wrong_types_are_rejected(self):
        f = make_factory(DEFAULTS_PROTO)
        with pytest.raises(TypeError):
            f.build_request("T.Api.play", {"next_index": "not a number"}, "t")
        with pytest.raises(TypeError):
            f.build_request("T.Api.play", {"tags": "not a list"}, "t")
        with pytest.raises(TypeError):
            f.build_request("T.Api.play", "not a dict", "t")

    def test_a_protobuf_message_instance_is_accepted(self):
        f = make_factory(DEFAULTS_PROTO)
        cls = f.root.message_class(f.root.lookup_type("T.Turn"))
        assert round_trip(f, cls(next_index=9))["next_index"] == 9


class TestCustomTypesInOnDiskProtos:
    @pytest.fixture
    def proto_dir(self, tmp_path):
        (tmp_path / "Wallet.proto").write_text(
            'syntax = "proto3";\npackage Wallet;\n'
            "message Balance { bigint amount = 1; timestamp as_of = 2; }\n"
            "message Query   { string account = 1; }\n"
            "service Api { rpc balance (Query) returns (Balance); }\n"
        )
        # Must be ignored by discovery: only *.proto should be loaded.
        (tmp_path / "notes.protocol.txt").write_text("not a proto file")
        (tmp_path / "Wallet.proto.bak").write_text('syntax = "proto3"; @@@ invalid @@@')
        return str(tmp_path)

    def test_loads_a_proto_that_uses_bigint_and_timestamp(self, proto_dir):
        f = MessageFactory()
        f.init([proto_dir])
        assert f.has_service("Wallet.Api")

    def test_round_trips_a_bigint_declared_in_an_on_disk_proto(self, proto_dir):
        f = MessageFactory()
        f.init([proto_dir])
        buf = f.build_response("Wallet.Api.balance", {"amount": 10**30})
        assert f.decode_response(buf).result.data["amount"] == 10**30

    def test_ignores_files_that_merely_contain_proto_in_the_name(self, proto_dir):
        f = MessageFactory()
        f.init([proto_dir])
        assert f.has_service("Wallet.Api")

    def test_loads_nested_directories_and_imports_in_any_order(self, tmp_path):
        (tmp_path / "svc").mkdir()
        (tmp_path / "svc" / "a.proto").write_text(
            'syntax = "proto3"; package A; import "common/types.proto";\n'
            "service Api { rpc go (Common.Req) returns (Common.Res); }"
        )
        (tmp_path / "common").mkdir()
        (tmp_path / "common" / "types.proto").write_text(
            'syntax = "proto3"; package Common; message Req { string x = 1; } message Res { string y = 1; }'
        )
        f = MessageFactory()
        f.init([str(tmp_path)])
        assert f.has_service("A.Api")
        assert f.decode_request(f.build_request("A.Api.go", {"x": "1"}, "a")).data == {"x": "1"}

    def test_a_missing_import_is_reported_rather_than_swallowed(self, tmp_path):
        (tmp_path / "a.proto").write_text('syntax = "proto3"; package A; service Api { rpc go (Nope.Req) returns (Nope.Req); }')
        with pytest.raises(ProtoParseError, match="Nope.Req"):
            MessageFactory().init([str(tmp_path)])

    def test_an_invalid_proto_is_reported_rather_than_swallowed(self, tmp_path):
        (tmp_path / "a.proto").write_text('syntax = "proto3"; package A; message { }')
        with pytest.raises(ProtoParseError):
            MessageFactory().init([str(tmp_path)])

    def test_a_missing_directory_is_an_error(self):
        with pytest.raises(FileNotFoundError):
            MessageFactory().init(["/nonexistent/protos"])

    def test_a_single_file_path_is_accepted(self, proto_dir):
        f = MessageFactory()
        f.init(os.path.join(proto_dir, "Wallet.proto"))
        assert f.has_service("Wallet.Api")


DUP = """
    syntax = "proto3";
    package Dup;
    message A { string x = 1; }
    service Svc { rpc go (A) returns (A); }
"""


class TestRegisteringAServiceSchemaMoreThanOnce:
    def test_is_idempotent(self):
        f = MessageFactory()
        f.init([])
        f.parse(DUP, "Dup.Svc")
        f.parse(DUP, "Dup.Svc")
        assert f.has_service("Dup.Svc")

    def test_reports_whether_a_service_is_already_registered(self):
        f = MessageFactory()
        f.init([])
        assert f.has_service("Dup.Svc") is False
        f.parse(DUP, "Dup.Svc")
        assert f.has_service("Dup.Svc") is True


class TestMessageServiceRegistersItsOwnSchema:
    PROTO = (
        'syntax = "proto3";\npackage Standalone;\n'
        "message Req { string x = 1; } message Res { string y = 1; }\n"
        "service Api { rpc go (Req) returns (Res); }\n"
    )

    @pytest.fixture
    def proto_path(self, tmp_path):
        path = tmp_path / "Standalone.proto"
        path.write_text(self.PROTO)
        return str(path)

    def build(self, factory, proto_path):
        class Standalone(MessageService):
            service_name = "Standalone.Api"
            proto_file_name = proto_path

            async def go(self, *_a):
                return {"y": "ok"}

        return Standalone(FakeContext(factory))

    async def test_registers_its_schema_during_init_when_nothing_else_has(self, proto_path):
        f = MessageFactory()
        f.init([])
        assert f.has_service("Standalone.Api") is False
        await self.build(f, proto_path).init()
        assert f.has_service("Standalone.Api") is True

    async def test_does_not_fail_when_the_schema_was_already_loaded_from_a_directory(self, proto_path):
        f = MessageFactory()
        f.init([os.path.dirname(proto_path)])
        assert f.has_service("Standalone.Api")
        await self.build(f, proto_path).init()

    async def test_can_be_initialised_twice_without_a_duplicate_name_error(self, proto_path):
        f = MessageFactory()
        f.init([])
        await self.build(f, proto_path).init()
        await self.build(f, proto_path).init()


COMBAT = """syntax = "proto3";
package Combat;
message PlayerJoined { string player_id = 1; }
message ShootRequest { string target_id = 1; }
message ShootResponse { bool hit = 1; }
service Player { rpc shoot (Combat.ShootRequest) returns (Combat.ShootResponse); }"""


class TestRepeatedSchemaRegistration:
    def test_treats_re_parsing_identical_schema_text_as_a_no_op(self):
        f = MessageFactory()
        f.init([])
        f.parse(COMBAT, "Combat.Player.player1")
        f.parse(COMBAT, "Combat.Player.player2")
        f.parse(COMBAT, "Combat.Player.player3")
        assert f.has_service("Combat.Player")

    def test_still_rejects_a_genuinely_conflicting_redefinition(self):
        f = MessageFactory()
        f.init([])
        f.parse(COMBAT, "Combat.Player.player1")
        conflicting = 'syntax = "proto3";\npackage Combat;\nmessage PlayerJoined { int32 completely_different = 1; }'
        with pytest.raises(Exception):
            f.parse(conflicting, "Combat.Other")

    def test_is_unaffected_when_the_service_name_does_match(self):
        f = MessageFactory()
        f.init([])
        f.parse(COMBAT, "Combat.Player")
        f.parse(COMBAT, "Combat.Player")


SILENT = """syntax = "proto3";
package Silent;
message Request { string a = 1; }
message Response { string b = 1; }
service Service { rpc go(Silent.Request) returns(Silent.Response); }"""


class TestParseBeforeInit:
    def test_raises_not_initialized_error_rather_than_parsing_into_a_discarded_root(self):
        with pytest.raises(NotInitializedError, match="init"):
            MessageFactory().parse(SILENT, "Silent.Service")

    def test_leaves_the_factory_untouched_rather_than_half_registering_the_schema(self):
        f = MessageFactory()
        with pytest.raises(NotInitializedError):
            f.parse(SILENT, "Silent.Service")
        f.init([])
        assert f.has_service("Silent.Service") is False
        f.parse(SILENT, "Silent.Service")
        assert f.has_service("Silent.Service") is True

    def test_build_before_init_raises(self):
        with pytest.raises(NotInitializedError):
            MessageFactory().build_request("Silent.Service.go", {}, "x")


DOTTED = """
syntax = "proto3";
package com.example.billing;
service Calculator {
  rpc add(AddRequest) returns (AddResponse);
  rpc addStream(AddRequest) returns (stream AddResponse);
}
message AddRequest { int32 a = 1; int32 b = 2; }
message AddResponse { int32 result = 1; }
"""


class TestMultiSegmentPackages:
    FULL = "com.example.billing.Calculator.add"

    @pytest.fixture
    def factory(self):
        return make_factory(DOTTED, "com.example.billing.Calculator")

    def test_builds_and_decodes_a_request(self, factory):
        decoded = factory.decode_request(factory.build_request(self.FULL, {"a": 2, "b": 3}, "tester"))
        assert decoded.method == self.FULL
        assert decoded.actor == "tester"
        assert decoded.data == {"a": 2, "b": 3}

    def test_builds_and_decodes_a_response(self, factory):
        assert factory.decode_response(factory.build_response(self.FULL, {"result": 5})).result.data == {"result": 5}

    def test_detects_a_streaming_method(self, factory):
        assert factory.is_streaming_method("com.example.billing.Calculator.addStream") is True
        assert factory.is_streaming_method(self.FULL) is False
        assert factory.is_streaming_method("nope") is False

    def test_names_the_method_not_the_parse_when_one_does_not_exist(self, factory):
        with pytest.raises(UnknownMethodError, match="nope"):
            factory.build_request("com.example.billing.Calculator.nope", {}, "x")

    def test_split_method_name_parses_from_the_right(self):
        assert MessageFactory.split_method_name("com.example.Calc.add") == ("com.example.Calc", "add")
        for bad in ("add", ".add", "add.", "", None):
            with pytest.raises(InvalidMethodNameError):
                MessageFactory.split_method_name(bad)

    def test_get_service_method_names_is_in_declaration_order(self, factory):
        assert factory.get_service_method_names("com.example.billing.Calculator") == ["add", "addStream"]

    def test_unknown_types_raise(self, factory):
        with pytest.raises(UnknownTypeError):
            factory.decode_message("com.example.Nope", b"")
        with pytest.raises(UnknownTypeError):
            factory.build_event("com.example.Nope", {}, "t")


class TestErrorResponses:
    def test_an_error_response_carries_message_and_code(self):
        f = make_factory(DUP, "Dup.Svc")
        out = f.decode_response(f.build_response("Dup.Svc.go", HandledError("bad", "CODE")))
        assert out.result is None
        assert (out.error.method, out.error.message, out.error.code) == ("Dup.Svc.go", "bad", "CODE")

    def test_an_error_without_a_code_has_an_empty_code(self):
        f = make_factory(DUP, "Dup.Svc")
        out = f.decode_response(f.build_response("Dup.Svc.go", RuntimeError("plain")))
        assert out.error.code == ""
        assert out.error.message == "plain"

    def test_an_error_needs_no_method_lookup(self):
        f = make_factory(DUP, "Dup.Svc")
        out = f.decode_response(f.build_response("Nope.Svc.missing", RuntimeError("x")))
        assert out.error.method == "Nope.Svc.missing"

    def test_an_empty_response_container_has_neither(self):
        f = make_factory(DUP, "Dup.Svc")
        from protobus.message_factory import _ResponseContainerMsg

        out = f.decode_response(_ResponseContainerMsg().SerializeToString())
        assert out.result is None and out.error is None


class TestEvents:
    def test_events_carry_type_topic_and_typed_payload(self):
        f = make_factory(DUP, "Dup.Svc")
        out = f.decode_event(f.build_event("Dup.A", {"x": "hello"}, "EVENT.Dup.A"))
        assert (out.type, out.topic, out.data) == ("Dup.A", "EVENT.Dup.A", {"x": "hello"})


PLAYER = """
syntax = "proto3";
package Combat;
service Player { rpc shoot(ShootRequest) returns (ShootResponse); }
message ShootRequest { string target = 1; }
message ShootResponse { bool hit = 1; }
"""


def build_player():
    f = make_factory(PLAYER, "Combat.Player")

    class Player(MessageService):
        service_name = "Combat.Player"
        proto_file_name = "Combat.proto"

        @property
        def Proto(self):
            return PLAYER

        async def shoot(self, *_a):
            return {"hit": True}

    return f, Player(FakeContext(f))


RK = MessageHandlerContext(signal=AbortController().signal, routing_key="REQUEST.Combat.Player.shoot")


class TestUndecodableInputIsAnsweredNotRetried:
    async def test_replies_with_a_protocol_error_instead_of_raising(self):
        f, svc = build_player()
        out = await svc._on_message(bytes([0xFF] * 5), "cid", {}, RK)
        reply = f.decode_response(out)
        assert reply.error is not None
        assert reply.error.code == "PROTOCOL_ERROR"

    async def test_answers_an_undecodable_payload_behind_a_valid_envelope(self):
        f, svc = build_player()
        envelope = _RequestContainerMsg(method="Combat.Player.shoot", actor="attacker", data=bytes([0xFF, 0xFF, 0xFF]))
        out = await svc._on_message(envelope.SerializeToString(), "cid", {}, RK)
        assert f.decode_response(out).error.code == "PROTOCOL_ERROR"

    async def test_still_treats_a_real_handler_failure_as_retryable(self):
        f, svc = build_player()

        async def shoot(*_a):
            raise RuntimeError("database is down")

        svc.shoot = shoot  # type: ignore[method-assign]
        body = f.build_request("Combat.Player.shoot", {"target": "x"}, "a")
        with pytest.raises(RuntimeError) as info:
            await svc._on_message(body, "cid", {}, RK)
        assert is_handled_error(info.value) is False

    def test_keeps_invalid_method_error_out_of_the_retry_ladder_too(self):
        assert is_handled_error(InvalidMethodError("nope")) is True
