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
def bigint_to_bytes(value: Union[int, str]) -> bytes:
    """
    Convert a bigint to 32-byte big-endian representation.

    Supports Web3/crypto applications with large integers.

    Args:
        value: Integer or string representation of the bigint

    Returns:
        32-byte big-endian bytes
    """
    if isinstance(value, str):
        # Handle hex strings
        if value.startswith("0x") or value.startswith("0X"):
            value = int(value, 16)
        else:
            value = int(value)

    # Convert to bytes, ensuring 32 bytes with big-endian encoding
    # Handle negative numbers with two's complement
    if value < 0:
        # Two's complement for 256 bits
        value = (1 << 256) + value

    byte_length = (value.bit_length() + 7) // 8
    byte_length = max(byte_length, 1)  # At least 1 byte

    raw_bytes = value.to_bytes(byte_length, byteorder="big", signed=False)

    # Pad or truncate to 32 bytes
    if len(raw_bytes) < 32:
        return b"\x00" * (32 - len(raw_bytes)) + raw_bytes
    return raw_bytes[-32:]


def bytes_to_bigint(data: bytes) -> int:
    """
    Convert bytes to a bigint.

    Args:
        data: Bytes to convert

    Returns:
        Integer value
    """
    if not data:
        return 0
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
