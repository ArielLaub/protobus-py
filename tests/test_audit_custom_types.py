"""
The bigint wire format is a fixed 32-byte unsigned big-endian field. Anything
that does not fit must be rejected, not quietly reshaped.

Parity with TS protobus 908d5c8 ("bigint silently took the absolute value and
truncated mod 2^256; -5n was stored as 5n. Now throws RangeError") and fdb74d3
("bound the bigint wire value at 32 bytes").

The Python port reshaped differently but just as silently: a negative was
two's-complemented into 256 bits and then decoded back as a huge positive, and
anything wider than 32 bytes was truncated to its low 32 with ``raw[-32:]``.
Both are lossy round-trips that no caller can detect.
"""

import pytest

from protobus.custom_types import bigint_to_bytes, bytes_to_bigint


class TestNegativeBigintIsRejected:
    def test_negative_does_not_round_trip(self):
        """The behaviour the fix removes: -5 came back as 2**256 - 5."""
        with pytest.raises(ValueError):
            bigint_to_bytes(-5)

    def test_negative_one_is_rejected(self):
        with pytest.raises(ValueError):
            bigint_to_bytes(-1)

    def test_negative_string_is_rejected(self):
        with pytest.raises(ValueError):
            bigint_to_bytes("-5")


class TestOversizeBigintIsRejected:
    def test_value_wider_than_32_bytes_is_rejected(self):
        with pytest.raises(ValueError):
            bigint_to_bytes(1 << 256)

    def test_largest_representable_value_still_encodes(self):
        value = (1 << 256) - 1
        assert bytes_to_bigint(bigint_to_bytes(value)) == value

    def test_decode_rejects_more_than_32_bytes(self):
        with pytest.raises(ValueError):
            bytes_to_bigint(b"\x01" * 33)


class TestRoundTripsThatMustKeepWorking:
    @pytest.mark.parametrize("value", [0, 1, 255, 256, 2**64, 2**255])
    def test_round_trip(self, value):
        encoded = bigint_to_bytes(value)
        assert len(encoded) == 32
        assert bytes_to_bigint(encoded) == value

    def test_hex_string_input(self):
        assert bytes_to_bigint(bigint_to_bytes("0xff")) == 255

    def test_decimal_string_input(self):
        assert bytes_to_bigint(bigint_to_bytes("123")) == 123

    def test_empty_bytes_decode_to_zero(self):
        assert bytes_to_bigint(b"") == 0
