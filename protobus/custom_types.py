"""
Custom scalar-like types for .proto schemas.

A custom type is a name (``bigint``, ``timestamp``, ``uuid`` …) that can be
used in a .proto file as if it were a scalar. On the wire it is a one-field
message ``{ <wire_type> value = 1; }`` — the same shape the TypeScript port
generates with protobufjs — so the two ports interoperate byte for byte.

Registration is **process-wide**, as it is in the TypeScript port: the codec
registry is module-level, so a type registered through one factory is known
to every factory. Only the addition of the wrapper message to a factory's
descriptor pool is per instance.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Generic, List, Literal, Optional, TypeVar, Union

from google.protobuf import descriptor_pb2

from .errors import CustomTypeConflictError

T = TypeVar("T")

WireType = Literal["bytes", "int64", "uint64", "string", "int32", "uint32", "double"]

_WIRE_TYPE_TO_FIELD_TYPE = {
    "bytes": descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
    "int64": descriptor_pb2.FieldDescriptorProto.TYPE_INT64,
    "uint64": descriptor_pb2.FieldDescriptorProto.TYPE_UINT64,
    "string": descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    "int32": descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
    "uint32": descriptor_pb2.FieldDescriptorProto.TYPE_UINT32,
    "double": descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE,
}


@dataclass
class CustomType(Generic[T]):
    """
    Definition of a custom type.

    Attributes:
        name: The name as it appears in .proto files. Lowercase, so it reads
            like a built-in scalar: ``bigint``, ``timestamp``, ``uuid``.
        wire_type: The protobuf scalar the value is carried as.
        encode: User value -> wire value (bytes / int / str / float).
        decode: Wire value -> user value.
        py_type: The Python type name used in generated code.
    """

    name: str
    wire_type: WireType
    encode: Callable[[Any], Any]
    decode: Callable[[Any], T]
    py_type: str = "Any"

    # TS parity: the TypeScript definition calls this field tsType.
    @property
    def ts_type(self) -> str:
        return self.py_type


# TS parity alias.
ICustomType = CustomType

# Process-wide registry of custom type implementations.
_custom_type_registry: Dict[str, CustomType] = {}


def wrapper_descriptor(custom_type: CustomType) -> descriptor_pb2.DescriptorProto:
    """
    The one-field wrapper message a custom type is carried as:
    ``message <name> { <wire_type> value = 1; }``.
    """
    if custom_type.wire_type not in _WIRE_TYPE_TO_FIELD_TYPE:
        raise ValueError(
            f"custom type '{custom_type.name}' has unsupported wire type "
            f"'{custom_type.wire_type}'"
        )
    message = descriptor_pb2.DescriptorProto(name=custom_type.name)
    message.field.add(
        name="value",
        number=1,
        type=_WIRE_TYPE_TO_FIELD_TYPE[custom_type.wire_type],
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
    )
    return message


def assert_no_wire_type_conflict(custom_type: CustomType) -> None:
    """
    Two registrations of one name are only interchangeable if they agree on
    the wire type: the wrapper message is fixed at first registration, so a
    second registration with a different wire type would go on encoding in
    the FIRST format while the caller believes it changed.
    """
    previous = _custom_type_registry.get(custom_type.name)
    if previous is not None and previous.wire_type != custom_type.wire_type:
        raise CustomTypeConflictError(
            f"custom type '{custom_type.name}' is already registered with wire type "
            f"'{previous.wire_type}'; re-registering it as '{custom_type.wire_type}' "
            "would keep encoding in the original wire format. Use a different name, "
            "or keep the original wire type."
        )


def register_custom_type(custom_type: CustomType) -> descriptor_pb2.DescriptorProto:
    """
    Register a custom type process-wide and return its wrapper descriptor.

    The last registration of a name wins for the codec; a definition that
    disagrees about the wire type is refused with CustomTypeConflictError.
    """
    assert_no_wire_type_conflict(custom_type)
    descriptor = wrapper_descriptor(custom_type)
    _custom_type_registry[custom_type.name] = custom_type
    return descriptor


def refresh_custom_type_codec(custom_type: CustomType) -> None:
    """
    Point the name at this definition's codec, leaving the wrapper message
    already generated for it alone. This is the half of registration that is
    safe to repeat.
    """
    assert_no_wire_type_conflict(custom_type)
    _custom_type_registry[custom_type.name] = custom_type


def get_custom_type(name: str) -> Optional[CustomType]:
    """A registered custom type by name, or None."""
    return _custom_type_registry.get(name)


def is_custom_type(name: Any) -> bool:
    """True if ``name`` is a registered custom type name."""
    return isinstance(name, str) and name in _custom_type_registry


def get_custom_type_names() -> List[str]:
    """Every registered custom type name."""
    return list(_custom_type_registry.keys())


# ===========================================================================
# Built-in custom types
# ===========================================================================

#: Width of the bigint wire format, in bytes.
BIGINT_BYTES = 32
BIGINT_WIRE_BYTES = BIGINT_BYTES  # 1.x name

#: Largest value representable in the 32-byte unsigned wire format.
BIGINT_MAX = (1 << (BIGINT_BYTES * 8)) - 1


def bigint_to_bytes(value: Union[int, str]) -> bytes:
    """
    Convert an integer to its 32-byte big-endian wire representation.

    The wire format is fixed-width and **unsigned**. Values outside
    ``[0, 2**256-1]`` are rejected rather than coerced — neither taking the
    absolute value nor truncating mod 2**256. For financial and on-chain
    amounts, failing loudly is the only safe behaviour. Matches the
    TypeScript port, which raises RangeError for the same inputs.

    Raises:
        ValueError: negative, wider than the wire format, or not an integer.
    """
    if isinstance(value, bool):
        raise ValueError(f"bigint value must be an integer, got {value!r}")
    if isinstance(value, str):
        text = value.strip()
        value = int(text, 16) if text[:2].lower() == "0x" else int(text)
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"bigint value {value!r} is not an integer")
        value = int(value)
    elif not isinstance(value, int):
        raise ValueError(f"bigint value must be an integer, got {type(value).__name__}")

    if value < 0:
        raise ValueError(
            f"bigint value {value} is negative; the protobus bigint wire format is "
            "unsigned (0 .. 2^256-1)"
        )
    if value > BIGINT_MAX:
        raise ValueError(
            f"bigint value {value} exceeds the maximum representable value 2^256-1"
        )
    return value.to_bytes(BIGINT_BYTES, byteorder="big", signed=False)


def bytes_to_bigint(data: Optional[bytes]) -> int:
    """
    Convert wire bytes to an integer.

    Anything wider than the wire format is rejected rather than decoded: the
    encoder never emits more than 32 bytes, so a wider value did not come from
    a peer speaking this protocol.
    """
    if not data:
        return 0
    if len(data) > BIGINT_BYTES:
        raise ValueError(
            f"bigint wire value is {len(data)} bytes; the protobus bigint wire "
            f"format is at most {BIGINT_BYTES}"
        )
    return int.from_bytes(bytes(data), byteorder="big", signed=False)


BigIntType: CustomType[int] = CustomType(
    name="bigint",
    wire_type="bytes",
    encode=lambda value: bigint_to_bytes(0 if value is None else value),
    decode=bytes_to_bigint,
    py_type="int",
)


def encode_timestamp(value: Union[datetime, int, float, str, None]) -> int:
    """
    Encode a timestamp as milliseconds since the Unix epoch.

    A timezone-aware datetime is exact. A naive datetime is interpreted in
    the process's local timezone, which is what ``datetime.timestamp()`` does;
    pass aware datetimes when the process timezone is not the one you mean.
    An ISO-8601 string is parsed with ``datetime.fromisoformat``.
    """
    if value is None:
        return 0
    if isinstance(value, datetime):
        return int(round(value.timestamp() * 1000))
    if isinstance(value, bool):
        raise ValueError("timestamp value must be a datetime or a number of milliseconds")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(round(datetime.fromisoformat(text).timestamp() * 1000))
    raise ValueError(f"cannot encode {type(value).__name__} as a timestamp")


def decode_timestamp(value: Any) -> datetime:
    """
    Decode milliseconds since the epoch to a timezone-aware UTC datetime.

    UTC rather than local so the value round-trips identically on every
    machine, and so it compares equal to what the TypeScript port's ``Date``
    represents — an absolute instant.
    """
    if value is None:
        millis = 0
    elif isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, str):
        millis = int(value)
    else:
        millis = int(value)
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)


TimestampType: CustomType[datetime] = CustomType(
    name="timestamp",
    wire_type="int64",
    encode=encode_timestamp,
    decode=decode_timestamp,
    py_type="datetime",
)

# Register the built-ins at import time, before any factory exists.
BigIntMessage = register_custom_type(BigIntType)
TimestampMessage = register_custom_type(TimestampType)
