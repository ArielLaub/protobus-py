"""Custom types: bigint and timestamp built-ins, user-defined types, maps,
nesting, idempotent registration, and per-factory ownership."""

import time
from datetime import datetime, timezone

import pytest

from protobus import (
    BIGINT_MAX,
    BigIntType,
    CustomType,
    CustomTypeConflictError,
    MessageFactory,
    TimestampType,
    bigint_to_bytes,
    bytes_to_bigint,
    get_custom_type,
    is_custom_type,
)

from ..helpers import make_factory


class TestBigintToBytes:
    def test_converts_zero(self):
        out = bigint_to_bytes(0)
        assert len(out) == 32 and all(b == 0 for b in out)

    def test_converts_small_numbers(self):
        out = bigint_to_bytes(255)
        assert out[31] == 255 and all(b == 0 for b in out[:31])

    def test_converts_in_big_endian_order(self):
        out = bigint_to_bytes(0x1234)
        assert (out[30], out[31]) == (0x12, 0x34)

    def test_handles_uint64_max(self):
        out = bigint_to_bytes(2**64 - 1)
        assert all(b == 0xFF for b in out[24:]) and all(b == 0 for b in out[:24])

    def test_handles_uint256_max(self):
        assert all(b == 0xFF for b in bigint_to_bytes(2**256 - 1))

    def test_accepts_string_input_decimal_and_hex(self):
        assert bytes_to_bigint(bigint_to_bytes("12345678901234567890")) == 12345678901234567890
        assert bytes_to_bigint(bigint_to_bytes("0xdeadbeef")) == 0xDEADBEEF

    def test_accepts_an_integral_float(self):
        assert bytes_to_bigint(bigint_to_bytes(42.0)) == 42
        with pytest.raises(ValueError):
            bigint_to_bytes(4.5)

    def test_rejects_bools_and_other_types(self):
        with pytest.raises(ValueError):
            bigint_to_bytes(True)
        with pytest.raises(ValueError):
            bigint_to_bytes([1])


class TestBytesToBigint:
    def test_handles_empty_and_none_input(self):
        assert bytes_to_bigint(b"") == 0
        assert bytes_to_bigint(None) == 0

    def test_converts_single_and_multi_byte(self):
        assert bytes_to_bigint(bytes([42])) == 42
        assert bytes_to_bigint(bytes([0x12, 0x34])) == 0x1234
        assert bytes_to_bigint(bytearray([0xAB, 0xCD])) == 0xABCD

    @pytest.mark.parametrize("value", [0, 1, 255, 256, 65535, 0x123456789ABCDEF0, 2**64 - 1, 2**128 - 1, 2**256 - 1])
    def test_round_trips(self, value):
        assert bytes_to_bigint(bigint_to_bytes(value)) == value


class TestBigintDecodeBounds:
    def test_accepts_the_full_32_byte_wire_width(self):
        encoded = BigIntType.encode(BIGINT_MAX)
        assert len(encoded) == 32
        assert BigIntType.decode(encoded) == BIGINT_MAX

    def test_accepts_a_short_encoding(self):
        assert BigIntType.decode(bytes([0x01, 0x00])) == 256
        assert BigIntType.decode(b"") == 0

    def test_rejects_anything_wider_than_the_wire_format(self):
        with pytest.raises(ValueError):
            BigIntType.decode(bytes(33))
        with pytest.raises(ValueError):
            bytes_to_bigint(bytes(64 * 1024))

    def test_cannot_be_made_to_spend_unbounded_cpu_on_one_value(self):
        started = time.perf_counter()
        with pytest.raises(ValueError):
            BigIntType.decode(bytes([0xFF]) * (1024 * 1024))
        assert (time.perf_counter() - started) < 0.05


class TestBigintRangeValidation:
    def test_encodes_the_full_unsigned_256_bit_range(self):
        mx = 2**256 - 1
        assert BigIntType.decode(BigIntType.encode(mx)) == mx
        assert BigIntType.decode(BigIntType.encode(0)) == 0

    def test_rejects_negative_values_instead_of_dropping_the_sign(self):
        with pytest.raises(ValueError):
            BigIntType.encode(-5)
        with pytest.raises(ValueError):
            BigIntType.encode("-1")

    def test_rejects_values_that_do_not_fit_in_256_bits(self):
        with pytest.raises(ValueError):
            BigIntType.encode(2**256)
        with pytest.raises(ValueError):
            BigIntType.encode(2**256 + 7)


BIGINT_PROTO = """
    syntax = "proto3";
    package TestBigInt;
    message TokenAmount { bigint amount = 1; string token = 2; }
    service TokenService { rpc transfer(TokenAmount) returns(TokenAmount); }
"""


class TestBigintProtoWrapperIntegration:
    @pytest.fixture
    def factory(self):
        return make_factory(BIGINT_PROTO)

    def rt(self, f, obj):
        return f.decode_request(f.build_request("TestBigInt.TokenService.transfer", obj, "test-actor")).data

    def test_encodes_and_decodes_a_bigint_field(self, factory):
        out = self.rt(factory, {"amount": 12345678901234567890, "token": "ETH"})
        assert out["amount"] == 12345678901234567890
        assert isinstance(out["amount"], int)
        assert out["token"] == "ETH"

    def test_handles_string_input_decimal_and_hex(self, factory):
        assert self.rt(factory, {"amount": "999999999999999999999"})["amount"] == 999999999999999999999
        assert self.rt(factory, {"amount": "0xffffffffffffffff"})["amount"] == 0xFFFFFFFFFFFFFFFF

    def test_handles_uint256_max_and_zero(self, factory):
        assert self.rt(factory, {"amount": 2**256 - 1})["amount"] == 2**256 - 1
        assert self.rt(factory, {"amount": 0})["amount"] == 0

    def test_an_unset_bigint_decodes_as_none(self, factory):
        assert self.rt(factory, {"token": "x"})["amount"] is None

    def test_surfaces_out_of_range_values_through_the_message_encode_path(self, factory):
        with pytest.raises(ValueError):
            factory.build_request("TestBigInt.TokenService.transfer", {"amount": -1}, "x")

    def test_exports_bigint_as_int(self, factory):
        assert "amount: Optional[int]" in factory.export_python("TestBigInt.TokenService")


TIMESTAMP_PROTO = """
    syntax = "proto3";
    package TestTimestamp;
    message Event { string name = 1; timestamp created_at = 2; timestamp updated_at = 3; }
    service EventService { rpc create(Event) returns(Event); }
"""


class TestTimestampProtoWrapperIntegration:
    @pytest.fixture
    def factory(self):
        return make_factory(TIMESTAMP_PROTO)

    def rt(self, f, obj):
        return f.decode_request(f.build_request("TestTimestamp.EventService.create", obj, "test-actor")).data

    def test_encodes_and_decodes_datetimes(self, factory):
        now = datetime.now(timezone.utc).replace(microsecond=123000)
        out = self.rt(factory, {"name": "test", "created_at": now, "updated_at": now})
        assert isinstance(out["created_at"], datetime)
        assert out["created_at"] == now
        assert out["created_at"].tzinfo is not None

    def test_accepts_iso_string_input(self, factory):
        out = self.rt(factory, {"created_at": "2024-01-15T10:30:00.000Z"})
        assert out["created_at"] == datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc)

    def test_accepts_milliseconds_number_input(self, factory):
        ms = int(time.time() * 1000)
        out = self.rt(factory, {"created_at": ms})
        assert int(out["created_at"].timestamp() * 1000) == ms

    def test_exports_timestamp_as_datetime(self, factory):
        source = factory.export_python("TestTimestamp.EventService")
        assert "created_at: Optional[datetime]" in source
        assert "from datetime import datetime" in source


class TestTimestampDecodeRange:
    @pytest.mark.parametrize("iso", [
        "1960-01-01T00:00:00.000+00:00",
        "1970-01-01T00:00:00.000+00:00",
        "1969-12-31T23:59:59.999+00:00",
        "2100-06-15T12:34:56.789+00:00",
    ])
    def test_round_trips(self, iso):
        when = datetime.fromisoformat(iso)
        assert TimestampType.decode(TimestampType.encode(when)) == when

    def test_a_naive_datetime_is_read_in_local_time(self):
        naive = datetime(2024, 1, 1, 12, 0, 0)
        assert TimestampType.encode(naive) == int(naive.timestamp() * 1000)


UUID_PROTO = """
    syntax = "proto3";
    package TestUUID;
    message Entity { uuid id = 1; string name = 2; }
    service EntityService { rpc get(Entity) returns(Entity); }
"""


class TestCustomTypeRegistration:
    def test_allows_registering_custom_types_before_init(self):
        f = MessageFactory()
        uuid_type = CustomType(
            name="uuid", wire_type="bytes",
            encode=lambda v: bytes.fromhex(v.replace("-", "")),
            decode=lambda d: f"{d.hex()[:8]}-{d.hex()[8:12]}-{d.hex()[12:16]}-{d.hex()[16:20]}-{d.hex()[20:]}",
            py_type="str",
        )
        f.register_type(uuid_type)
        f.init([])
        f.parse(UUID_PROTO)
        test_uuid = "550e8400-e29b-41d4-a716-446655440000"
        out = f.decode_request(f.build_request("TestUUID.EntityService.get", {"id": test_uuid, "name": "Test Entity"}, "a")).data
        assert out == {"id": test_uuid, "name": "Test Entity"}

    def test_supports_registering_types_after_init(self):
        f = MessageFactory()
        f.init([])
        f.register_type(CustomType(name="boolint", wire_type="int32", encode=lambda v: 1 if v else 0, decode=lambda d: d != 0, py_type="bool"))
        f.parse("""
            syntax = "proto3";
            package TestBoolInt;
            message Flags { boolint active = 1; boolint verified = 2; }
            service FlagService { rpc update(Flags) returns(Flags); }
        """)
        out = f.decode_request(f.build_request("TestBoolInt.FlagService.update", {"active": True, "verified": False}, "a")).data
        assert out == {"active": True, "verified": False}

    def test_exports_custom_type_with_its_python_type(self):
        f = MessageFactory()
        f.register_type(CustomType(name="intarray", wire_type="bytes", encode=bytes, decode=list, py_type="List[int]"))
        f.init([])
        f.parse("""
            syntax = "proto3";
            package TestArray;
            message Data { intarray values = 1; }
            service DataService { rpc process(Data) returns(Data); }
        """)
        assert "values: Optional[List[int]]" in f.export_python("TestArray.DataService")

    def test_string_wire_types_work(self):
        f = MessageFactory()
        f.init([])
        f.register_type(CustomType(name="upper", wire_type="string", encode=str.upper, decode=str.lower, py_type="str"))
        f.parse('syntax="proto3"; package U; message M { upper v = 1; } service S { rpc go(M) returns(M); }')
        assert f.decode_request(f.build_request("U.S.go", {"v": "Hi"}, "a")).data == {"v": "hi"}

    def test_a_pre_encoded_wire_value_is_accepted(self):
        f = make_factory(BIGINT_PROTO)
        raw = bigint_to_bytes(7)
        out = f.decode_request(f.build_request("TestBigInt.TokenService.transfer", {"amount": raw}, "a")).data
        assert out["amount"] == 7
        out = f.decode_request(f.build_request("TestBigInt.TokenService.transfer", {"amount": {"value": raw}}, "a")).data
        assert out["amount"] == 7


FIN = """
syntax = "proto3";
package Fin;
message Money   { bigint amount = 1; string currency = 2; }
message Line    { Money price = 1; string sku = 2; }
message Order   { Line lines = 1; Money total = 2; timestamp placed_at = 3; }
message Deep    { Order order = 1; }
message TopUp   { bigint amount = 1; }
message Tree    { string v = 1; Tree child = 2; }
service Api {
  rpc topup  (TopUp) returns (TopUp);
  rpc money  (Money) returns (Money);
  rpc order  (Order) returns (Order);
  rpc deep   (Deep)  returns (Deep);
  rpc walk   (Tree)  returns (Tree);
}
"""


def fin_rt(f, method, payload):
    return f.decode_request(f.build_request(f"Fin.Api.{method}", payload, "test-actor")).data


class TestCustomTypesNestedInsideASubMessage:
    def test_round_trips_a_top_level_bigint(self):
        assert fin_rt(make_factory(FIN), "topup", {"amount": 42})["amount"] == 42

    def test_round_trips_a_bigint_one_two_and_three_levels_deep(self):
        f = make_factory(FIN)
        assert fin_rt(f, "order", {"total": {"amount": 1234567890123456789, "currency": "USD"}})["total"]["amount"] == 1234567890123456789
        assert fin_rt(f, "order", {"lines": {"price": {"amount": 999, "currency": "USD"}, "sku": "ABC"}})["lines"]["price"]["amount"] == 999
        assert fin_rt(f, "deep", {"order": {"total": {"amount": 7, "currency": "EUR"}}})["order"]["total"]["amount"] == 7

    def test_round_trips_a_nested_timestamp(self):
        when = datetime(2026, 8, 3, 10, tzinfo=timezone.utc)
        assert fin_rt(make_factory(FIN), "order", {"placed_at": when})["placed_at"] == when

    def test_encodes_a_self_referential_message(self):
        out = fin_rt(make_factory(FIN), "walk", {"v": "x", "child": {"v": "y"}})
        assert out == {"v": "x", "child": {"v": "y", "child": None}}

    def test_two_factories_do_not_share_state(self):
        a = make_factory(FIN)
        fin_rt(a, "order", {"total": {"amount": 1, "currency": "USD"}})
        b = make_factory(FIN)
        assert fin_rt(b, "order", {"total": {"amount": 2, "currency": "USD"}})["total"]["amount"] == 2


MAPS = """syntax="proto3"; package Maps;
    message Holder { bigint amount = 1; }
    message Req {
        map<string, bigint> values = 1;
        map<int32, bigint> by_index = 2;
        map<string, Holder> holders = 3;
        map<string, string> labels = 4;
        bigint total = 5;
        repeated bigint history = 6;
    }
    service Calc { rpc add(Maps.Req) returns(Maps.Req); }"""


class TestCustomTypesInsideMapFields:
    @pytest.fixture
    def rt(self):
        f = make_factory(MAPS)
        return lambda obj: f.decode_request(f.build_request("Maps.Calc.add", obj, "test")).data

    def test_converts_each_value_of_a_custom_type_map(self, rt):
        assert rt({"values": {"x": 42, "y": 7}})["values"] == {"x": 42, "y": 7}

    def test_preserves_keys_including_numeric_ones(self, rt):
        out = rt({"by_index": {1: 10, 2: 20}})
        assert out["by_index"] == {1: 10, 2: 20}

    def test_converts_custom_fields_of_messages_held_in_a_map(self, rt):
        out = rt({"holders": {"a": {"amount": 5}, "b": {"amount": 6}}})
        assert out["holders"] == {"a": {"amount": 5}, "b": {"amount": 6}}

    def test_leaves_a_plain_map_alone(self, rt):
        assert rt({"labels": {"a": "one"}, "total": 1})["labels"] == {"a": "one"}

    def test_round_trips_an_empty_map(self, rt):
        assert rt({"values": {}, "total": 1})["values"] == {}

    def test_keeps_scalar_and_repeated_custom_type_fields_working(self, rt):
        out = rt({"total": 99, "history": [1, 2, 3]})
        assert out["total"] == 99 and out["history"] == [1, 2, 3]

    def test_carries_a_large_value_through_a_map_without_loss(self, rt):
        big = 2**200 + 12345
        assert rt({"values": {"big": big}})["values"]["big"] == big


def _uuid_type(name="idem_uuid"):
    return CustomType(name=name, wire_type="bytes", encode=lambda v: bytes.fromhex(v.replace("-", "")), decode=lambda d: d.hex(), py_type="str")


class TestRegisterTypeIsIdempotent:
    def test_re_registering_a_built_in_does_not_raise(self):
        f = MessageFactory()
        f.init([])
        f.register_type(BigIntType)
        f.register_type(TimestampType)

    def test_a_second_factory_can_register_the_same_custom_type_and_neither_loses_anything(self):
        a = MessageFactory()
        a.init([])
        a.register_type(_uuid_type())
        b = MessageFactory()
        b.init([])
        b.register_type(_uuid_type())
        assert a.has_type("idem_uuid") and b.has_type("idem_uuid")
        assert a.has_type("bigint") and a.has_type("timestamp")

    def test_a_later_registration_still_replaces_the_codec(self):
        f = MessageFactory()
        f.init([])
        f.register_type(CustomType(name="idem_codec", wire_type="string", encode=lambda v: f"first:{v}", decode=lambda d: f"first:{d}", py_type="str"))
        f.register_type(CustomType(name="idem_codec", wire_type="string", encode=lambda v: f"second:{v}", decode=lambda d: f"second:{d}", py_type="str"))
        assert get_custom_type("idem_codec").encode("x") == "second:x"

    def test_refuses_a_re_registration_that_changes_the_wire_type(self):
        f = MessageFactory()
        f.init([])
        f.register_type(CustomType(name="idem_conflict", wire_type="bytes", encode=bytes, decode=bytes, py_type="bytes"))
        with pytest.raises(CustomTypeConflictError, match="wire type"):
            f.register_type(CustomType(name="idem_conflict", wire_type="string", encode=str, decode=str, py_type="str"))

    def test_is_custom_type_answers_for_registered_names(self):
        assert is_custom_type("bigint") and is_custom_type("timestamp")
        assert not is_custom_type("int32") and not is_custom_type(None)


def _schema_using(pkg):
    return f"""syntax = "proto3";
package {pkg};
message Amount {{ bigint value = 1; }}
message Ack {{ bool ok = 1; }}
service Wallet {{ rpc credit({pkg}.Amount) returns({pkg}.Ack); }}"""


class TestBuiltInCustomTypesAreOwnedPerFactory:
    def test_a_second_factory_init_does_not_remove_them_from_the_first(self):
        a = MessageFactory()
        a.init([])
        b = MessageFactory()
        b.init([])
        for f in (a, b):
            assert f.has_type("bigint") and f.has_type("timestamp")

    def test_the_first_factory_can_still_parse_and_encode_afterwards(self):
        a = MessageFactory()
        a.init([])
        MessageFactory().init([])
        a.parse(_schema_using("OwnA"), "OwnA.Wallet")
        buffer = a.build_request("OwnA.Wallet.credit", {"value": 42}, "actor")
        envelope = a.decode_request_envelope(buffer)
        assert a.decode_request_payload(envelope.method, envelope.data) == {"value": 42}

    def test_both_factories_can_encode_against_their_own_copy_independently(self):
        a = MessageFactory()
        a.init([])
        b = MessageFactory()
        b.init([])
        a.parse(_schema_using("OwnB1"), "OwnB1.Wallet")
        b.parse(_schema_using("OwnB2"), "OwnB2.Wallet")
        a.build_request("OwnB1.Wallet.credit", {"value": 7}, "x")
        b.build_request("OwnB2.Wallet.credit", {"value": 9}, "x")

    def test_holds_for_a_user_defined_type_registered_on_two_factories(self):
        money = CustomType(name="own_money", wire_type="string", encode=str, decode=str, py_type="str")
        a = MessageFactory()
        a.init([])
        a.register_type(money)
        b = MessageFactory()
        b.init([])
        b.register_type(money)
        assert a.has_type("own_money") and b.has_type("own_money")

    def test_a_fresh_init_discards_the_previous_root(self):
        a = MessageFactory()
        a.init([])
        a.parse(_schema_using("Fresh"), "Fresh.Wallet")
        a.init([])
        assert not a.has_service("Fresh.Wallet")
        a.parse(_schema_using("Fresh"), "Fresh.Wallet")
        assert a.has_service("Fresh.Wallet")
