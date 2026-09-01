"""Custom type system for Protocol Buffers in Protobus."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Generic, List, Literal, Optional, TypeVar, Union

T = TypeVar("T")

WireType = Literal["bytes", "int64", "uint64", "string", "int32", "uint32", "double"]


@dataclass
class CustomType(Generic[T]):
    """
    Definition for a custom protobuf type.

    Attributes:
        name: Lowercase type name (e.g., 'bigint', 'timestamp')
        wire_type: Protobuf wire type for encoding
        encode: Function to convert user value to wire format
        decode: Function to convert wire format back to desired type
        ts_type: TypeScript type name for code generation (Python equivalent)
    """

    name: str
    wire_type: WireType
    encode: Callable[[Any], Any]
    decode: Callable[[Any], T]
    py_type: str  # Python type string for code generation


# Global registry of custom types
_custom_types: Dict[str, CustomType] = {}


def register_custom_type(custom_type: CustomType) -> None:
    """
    Register a custom type globally.

    Args:
        custom_type: The custom type definition to register
    """
    _custom_types[custom_type.name.lower()] = custom_type


def get_custom_type(name: str) -> Optional[CustomType]:
    """
    Get a registered custom type by name.

    Args:
        name: The type name to look up

    Returns:
        The custom type definition, or None if not found
    """
    return _custom_types.get(name.lower())


def is_custom_type(name: str) -> bool:
    """
    Check if a name is a registered custom type.

    Args:
        name: The type name to check

    Returns:
        True if the type is registered, False otherwise
    """
    return name.lower() in _custom_types


def get_custom_type_names() -> List[str]:
    """
    Get all registered custom type names.

    Returns:
        List of registered type names
    """
    return list(_custom_types.keys())


# BigInt utilities
BIGINT_WIRE_BYTES = 32
BIGINT_MAX = (1 << (BIGINT_WIRE_BYTES * 8)) - 1


def bigint_to_bytes(value: Union[int, str]) -> bytes:
    """
    Convert a bigint to its 32-byte big-endian wire representation.

    The wire format is fixed-width and *unsigned*. A value that does not fit
    is rejected rather than reshaped: the previous code two's-complemented a
    negative into 256 bits, which the unsigned decoder then read back as a
    vast positive (-5 became 2**256 - 5), and truncated anything wider than 32
    bytes to its low 32 (2**256 + 7 became 7). Both losses were undetectable
    by any caller.

    Matches TS protobus, which raises RangeError for the same two inputs.

    Args:
        value: Non-negative integer, or a decimal/hex string form of one

    Returns:
        32-byte big-endian bytes

    Raises:
        ValueError: If the value is negative or wider than the wire format
    """
    if isinstance(value, str):
        # Handle hex strings
        if value.startswith("0x") or value.startswith("0X"):
            value = int(value, 16)
        else:
            value = int(value)

    if value < 0:
        raise ValueError(
            f"bigint must be non-negative; the wire format is unsigned "
            f"(got {value})"
        )

    if value > BIGINT_MAX:
        raise ValueError(
            f"bigint exceeds the {BIGINT_WIRE_BYTES}-byte wire format "
            f"(got a {(value.bit_length() + 7) // 8}-byte value)"
        )

    return value.to_bytes(BIGINT_WIRE_BYTES, byteorder="big", signed=False)


def bytes_to_bigint(data: bytes) -> int:
    """
    Convert wire bytes to a bigint.

    Anything wider than the wire format is rejected rather than decoded: this
    encoder only ever produces 32 bytes, so a wider value did not come from a
    peer speaking this protocol.

    Args:
        data: Bytes to convert

    Returns:
        Integer value

    Raises:
        ValueError: If the input is wider than the wire format
    """
    if not data:
        return 0
    if len(data) > BIGINT_WIRE_BYTES:
        raise ValueError(
            f"bigint wire value is {len(data)} bytes; the format is "
            f"{BIGINT_WIRE_BYTES}"
        )
    return int.from_bytes(data, byteorder="big", signed=False)


# Built-in BigInt type
BigIntType = CustomType[int](
    name="bigint",
    wire_type="bytes",
    encode=lambda value: bigint_to_bytes(value if value is not None else 0),
    decode=lambda data: bytes_to_bigint(data) if data else 0,
    py_type="int",
)


# Built-in Timestamp type
def encode_timestamp(value: Union[datetime, int, float, None]) -> int:
    """Encode a timestamp value to milliseconds since epoch."""
    if value is None:
        return 0
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def decode_timestamp(value: Any) -> datetime:
    """Decode milliseconds since epoch to a datetime."""
    if value is None or value == 0:
        return datetime.fromtimestamp(0)
    # Handle protobuf Long types or regular integers
    if hasattr(value, "toNumber"):
        value = value.toNumber()
    elif hasattr(value, "low") and hasattr(value, "high"):
        # Handle protobuf Long object
        value = value.low + (value.high << 32)
    return datetime.fromtimestamp(int(value) / 1000)


TimestampType = CustomType[datetime](
    name="timestamp",
    wire_type="int64",
    encode=encode_timestamp,
    decode=decode_timestamp,
    py_type="datetime",
)


# Register built-in types
register_custom_type(BigIntType)
register_custom_type(TimestampType)
