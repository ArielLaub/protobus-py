"""Tests for custom types."""

import pytest
from datetime import datetime

from protobus.custom_types import (
    bigint_to_bytes,
    bytes_to_bigint,
    register_custom_type,
    get_custom_type,
    is_custom_type,
    CustomType,
    BigIntType,
    TimestampType,
)


class TestBigInt:
    """Test cases for BigInt type."""

    def test_small_number(self):
        """Test encoding/decoding small numbers."""
        value = 12345
        encoded = bigint_to_bytes(value)
        decoded = bytes_to_bigint(encoded)
        assert decoded == value

    def test_large_number(self):
        """Test encoding/decoding large numbers."""
        value = 2**200 + 12345
        encoded = bigint_to_bytes(value)
        decoded = bytes_to_bigint(encoded)
        assert decoded == value

    def test_zero(self):
        """Test encoding/decoding zero."""
        value = 0
        encoded = bigint_to_bytes(value)
        decoded = bytes_to_bigint(encoded)
        assert decoded == value

    def test_hex_string(self):
        """Test encoding from hex string."""
        value = "0xDEADBEEF"
        encoded = bigint_to_bytes(value)
        decoded = bytes_to_bigint(encoded)
        assert decoded == 0xDEADBEEF

    def test_decimal_string(self):
        """Test encoding from decimal string."""
        value = "12345678901234567890"
        encoded = bigint_to_bytes(value)
        decoded = bytes_to_bigint(encoded)
        assert decoded == 12345678901234567890

    def test_32_byte_output(self):
        """Test that output is always 32 bytes."""
        for value in [0, 1, 255, 2**64, 2**200]:
            encoded = bigint_to_bytes(value)
            assert len(encoded) == 32


class TestTimestamp:
    """Test cases for Timestamp type."""

    def test_encode_datetime(self):
        """Test encoding datetime objects."""
        dt = datetime(2023, 6, 15, 12, 30, 45)
        encoded = TimestampType.encode(dt)
        assert isinstance(encoded, int)
        assert encoded == int(dt.timestamp() * 1000)

    def test_decode_timestamp(self):
        """Test decoding timestamps."""
        ts = 1686835845000  # Some timestamp in ms
        decoded = TimestampType.decode(ts)
        assert isinstance(decoded, datetime)

    def test_roundtrip(self):
        """Test encode/decode roundtrip."""
        original = datetime(2023, 6, 15, 12, 30, 45)
        encoded = TimestampType.encode(original)
        decoded = TimestampType.decode(encoded)
        # Note: microseconds may be lost due to ms precision
        assert abs((decoded - original).total_seconds()) < 1

    def test_encode_int(self):
        """Test encoding integer timestamps."""
        ts = 1686835845000
        encoded = TimestampType.encode(ts)
        assert encoded == ts


class TestCustomTypeRegistry:
    """Test cases for custom type registry."""

    def test_builtin_types_registered(self):
        """Test that built-in types are registered."""
        assert is_custom_type("bigint")
        assert is_custom_type("timestamp")

    def test_get_custom_type(self):
        """Test getting registered types."""
        bigint = get_custom_type("bigint")
        assert bigint is not None
        assert bigint.name == "bigint"

    def test_case_insensitive(self):
        """Test case-insensitive type lookup."""
        assert is_custom_type("BigInt")
        assert is_custom_type("BIGINT")
        assert get_custom_type("BigInt") is not None

    def test_register_custom_type(self):
        """Test registering new custom types."""
        custom = CustomType[str](
            name="mytype",
            wire_type="string",
            encode=lambda x: x.upper(),
            decode=lambda x: x.lower(),
            py_type="str",
        )
        register_custom_type(custom)

        assert is_custom_type("mytype")
        retrieved = get_custom_type("mytype")
        assert retrieved is not None
        assert retrieved.encode("hello") == "HELLO"
        assert retrieved.decode("HELLO") == "hello"
