"""The built-in scalars refuse input they cannot represent.

Parity with TypeScript protobus 2.5.0 (protobus#25): a value that would be
encoded as something else must raise instead. A float has already lost
precision above 2**53 before ``bigint`` sees it, and a fractional millisecond
was silently truncated by ``int()``. A timestamp beyond the range a
JavaScript ``Date`` holds (±8.64e15 ms) would not decode on a TypeScript peer.
"""

import math
from datetime import datetime, timezone

import pytest

from protobus.custom_types import bigint_to_bytes, bytes_to_bigint, encode_timestamp


class TestTimestampRefusesWhatItCannotRepresent:
    @pytest.mark.parametrize(
        "value",
        [1.5, math.nan, math.inf, -math.inf, 8.64e15 + 1, -(8.64e15 + 1), 8_640_000_000_000_001],
        ids=["fraction", "nan", "inf", "-inf", "beyond-date-range", "before-date-range", "int-beyond-range"],
    )
    def test_rejects(self, value):
        with pytest.raises(ValueError, match="timestamp"):
            encode_timestamp(value)

    def test_names_what_to_pass_instead(self):
        with pytest.raises(ValueError, match="datetime|milliseconds"):
            encode_timestamp(1.5)

    @pytest.mark.parametrize(
        "value, want",
        [
            (datetime(2020, 1, 1, tzinfo=timezone.utc), 1577836800000),
            ("2020-01-01T00:00:00Z", 1577836800000),
            (1577836800000, 1577836800000),
            (1577836800000.0, 1577836800000),
            (-14182940000, -14182940000),
            (0, 0),
            (8_640_000_000_000_000, 8_640_000_000_000_000),
        ],
        ids=["datetime", "iso", "int-ms", "integral-float", "pre-1970", "epoch", "date-range-limit"],
    )
    def test_still_accepts(self, value, want):
        assert encode_timestamp(value) == want


class TestBigintRefusesAFloatThatHasLostPrecision:
    def test_rejects_a_float_above_2_53(self):
        with pytest.raises(ValueError, match="int or a decimal string"):
            bigint_to_bytes(2.0**60 + 1)

    @pytest.mark.parametrize(
        "value",
        [float(2**53 - 1), 0.0, 12.0, 2**200, "9007199254740993"],
        ids=["max-safe-float", "zero-float", "small-float", "huge-int", "decimal-string"],
    )
    def test_still_accepts(self, value):
        bigint_to_bytes(value)

    def test_an_int_beyond_2_53_round_trips_exactly(self):
        assert bytes_to_bigint(bigint_to_bytes(9007199254740993)) == 9007199254740993
