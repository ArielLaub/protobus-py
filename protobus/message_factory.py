"""
Message factory: schema loading and the protobuf wire envelope.

Wire-compatible with the TypeScript port. Every message on the bus is one of
five container messages; the inner ``data`` bytes are the service-specific
message encoded against the loaded .proto definitions::

  RequestContainer  { string method = 1; string actor = 2; bytes data = 3; }
  ResponseResult    { string method = 1; bytes data = 2; }
  ResponseError     { string method = 1; string message = 2; string code = 3; }
  ResponseContainer { ResponseResult result = 1; ResponseError error = 2; }
  EventContainer    { string type = 1; string topic = 2; bytes data = 3; }

Schemas are parsed at runtime by ``proto_parser`` — no ``protoc`` needed —
into a per-factory ``DescriptorPool``. Decoded values are plain Python:
``dict`` for messages, ``list`` for repeated fields, ``dict`` for maps,
``int`` for every integer width (64-bit included — Python holds the range
natively, where the TypeScript port must fall back to decimal strings),
``bytes`` for bytes, the value *name* for enums, and whatever the registered
codec produces for a custom type (``int`` for bigint, ``datetime`` for
timestamp). Proto3 scalar defaults are materialised, so a legitimate ``0``,
``""`` or ``False`` is present rather than missing.
"""

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, Type, Union

from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf.descriptor import (
    Descriptor,
    EnumDescriptor,
    FieldDescriptor,
    MethodDescriptor,
    ServiceDescriptor,
)
from google.protobuf.message import Message
from google.protobuf.message_factory import GetMessageClass

from .custom_types import (
    CustomType,
    get_custom_type,
    get_custom_type_names,
    is_custom_type,
    refresh_custom_type_codec,
    register_custom_type,
    wrapper_descriptor,
)
from .errors import (
    InvalidMethodNameError,
    MessageTypeRequiredError,
    NotInitializedError,
    UnknownMethodError,
)
from .logger import Logger
from .proto_parser import CUSTOM_TYPES_FILE, ProtoParseError, custom_types_file, parse_proto


class UnknownTypeError(LookupError):
    """No message or enum with that name is in the factory's root."""


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


@dataclass
class RequestContainer:
    """A decoded request. ``data`` is the decoded payload — or, from
    ``decode_request_envelope``, the still-encoded bytes."""

    method: str
    actor: str
    data: Any


@dataclass
class ResponseResult:
    method: str
    data: Any


@dataclass
class ResponseError:
    method: str
    message: str
    code: str


@dataclass
class ResponseContainer:
    """Exactly one of ``result`` and ``error`` is set."""

    result: Optional[ResponseResult] = None
    error: Optional[ResponseError] = None


@dataclass
class EventContainer:
    type: str
    topic: str
    data: Any


# ---------------------------------------------------------------------------
# The container schema, built once at import time (no protoc)
# ---------------------------------------------------------------------------

_CONTAINER_FILE = "protobus/containers.proto"


def _build_container_classes() -> Dict[str, Type[Message]]:
    fdp = descriptor_pb2.FileDescriptorProto(name=_CONTAINER_FILE, syntax="proto3")
    F = descriptor_pb2.FieldDescriptorProto

    def message(name: str, *fields: Tuple[str, int, int, str]) -> None:
        msg = fdp.message_type.add(name=name)
        for fname, number, ftype, type_name in fields:
            fld = msg.field.add(name=fname, number=number, type=ftype, label=F.LABEL_OPTIONAL)
            if type_name:
                fld.type_name = type_name

    message("RequestContainer", ("method", 1, F.TYPE_STRING, ""), ("actor", 2, F.TYPE_STRING, ""), ("data", 3, F.TYPE_BYTES, ""))
    message("ResponseResult", ("method", 1, F.TYPE_STRING, ""), ("data", 2, F.TYPE_BYTES, ""))
    message("ResponseError", ("method", 1, F.TYPE_STRING, ""), ("message", 2, F.TYPE_STRING, ""), ("code", 3, F.TYPE_STRING, ""))
    message("ResponseContainer", ("result", 1, F.TYPE_MESSAGE, ".ResponseResult"), ("error", 2, F.TYPE_MESSAGE, ".ResponseError"))
    message("EventContainer", ("type", 1, F.TYPE_STRING, ""), ("topic", 2, F.TYPE_STRING, ""), ("data", 3, F.TYPE_BYTES, ""))

    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(fdp.SerializeToString())
    return {name: GetMessageClass(pool.FindMessageTypeByName(name)) for name in
            ("RequestContainer", "ResponseResult", "ResponseError", "ResponseContainer", "EventContainer")}


_CONTAINERS = _build_container_classes()
_RequestContainerMsg = _CONTAINERS["RequestContainer"]
_ResponseResultMsg = _CONTAINERS["ResponseResult"]
_ResponseErrorMsg = _CONTAINERS["ResponseError"]
_ResponseContainerMsg = _CONTAINERS["ResponseContainer"]
_EventContainerMsg = _CONTAINERS["EventContainer"]


# ---------------------------------------------------------------------------
# Root: a DescriptorPool with lookups the rest of the library uses
# ---------------------------------------------------------------------------


def _well_known_files() -> List[descriptor_pb2.FileDescriptorProto]:
    """
    The google.protobuf well-known types, so a schema can reference
    ``google.protobuf.Timestamp`` and friends without shipping the files.
    """
    from google.protobuf import (
        any_pb2,
        duration_pb2,
        empty_pb2,
        field_mask_pb2,
        struct_pb2,
        timestamp_pb2,
        wrappers_pb2,
    )

    files = []
    for module in (any_pb2, duration_pb2, empty_pb2, field_mask_pb2, struct_pb2, timestamp_pb2, wrappers_pb2):
        fdp = descriptor_pb2.FileDescriptorProto()
        fdp.ParseFromString(module.DESCRIPTOR.serialized_pb)
        files.append(fdp)
    return files


class Root:
    """
    A factory's schema: a private ``DescriptorPool`` plus the lookups the
    library needs. The counterpart of protobufjs's ``Root`` in the TypeScript
    port, which is why ``factory.root`` exists as a public attribute.
    """

    def __init__(self) -> None:
        self.pool = descriptor_pool.DescriptorPool()
        self._classes: Dict[str, Type[Message]] = {}
        self.files: Dict[str, descriptor_pb2.FileDescriptorProto] = {}
        for fdp in _well_known_files():
            self._add_file(fdp)
        self._add_file(custom_types_file())

    # -- adding schema ------------------------------------------------------

    def _add_file(self, fdp: descriptor_pb2.FileDescriptorProto) -> None:
        # Explicit `import` lines that resolve to nothing in this pool are
        # dropped rather than fatal: every reference the file actually makes
        # was already resolved and recorded as a dependency by the parser.
        keep = [dep for dep in fdp.dependency if dep in self.files or dep == fdp.name]
        if list(fdp.dependency) != keep:
            del fdp.dependency[:]
            fdp.dependency.extend(keep)
            del fdp.public_dependency[:]
            del fdp.weak_dependency[:]
        self.pool.AddSerializedFile(fdp.SerializeToString())
        self.files[fdp.name] = fdp

    def lookup_kind(self, full_name: str) -> Optional[Tuple[str, str]]:
        """('message' | 'enum', file name) for a fully-qualified name, or None."""
        try:
            desc = self.pool.FindMessageTypeByName(full_name)
            return "message", desc.file.name
        except KeyError:
            pass
        try:
            desc = self.pool.FindEnumTypeByName(full_name)
            return "enum", desc.file.name
        except KeyError:
            return None

    def add_proto(self, text: str, file_name: str) -> descriptor_pb2.FileDescriptorProto:
        """Parse and add one .proto text. Raises ProtoParseError / TypeError."""
        fdp = parse_proto(text, file_name, self.lookup_kind)
        self._add_file(fdp)
        return fdp

    def add_custom_type(self, custom_type: CustomType) -> None:
        """
        Make a custom type registered after this root was built visible in
        it. The synthetic custom-types file cannot be re-added, so the wrapper
        goes into a file of its own.
        """
        if self.has_type(custom_type.name):
            return
        fdp = descriptor_pb2.FileDescriptorProto(
            name=f"protobus/custom_types/{custom_type.name}.proto", syntax="proto3"
        )
        fdp.message_type.append(wrapper_descriptor(custom_type))
        self._add_file(fdp)

    # -- lookups ------------------------------------------------------------

    def has_type(self, full_name: str) -> bool:
        try:
            self.pool.FindMessageTypeByName(full_name)
            return True
        except KeyError:
            return False

    def has_service(self, full_name: str) -> bool:
        try:
            self.pool.FindServiceByName(full_name)
            return True
        except KeyError:
            return False

    def lookup_type(self, full_name: str) -> Descriptor:
        try:
            return self.pool.FindMessageTypeByName(full_name)
        except KeyError:
            raise UnknownTypeError(f"no such type: '{full_name}'") from None

    def lookup_enum(self, full_name: str) -> EnumDescriptor:
        try:
            return self.pool.FindEnumTypeByName(full_name)
        except KeyError:
            raise UnknownTypeError(f"no such enum: '{full_name}'") from None

    def lookup_service(self, full_name: str) -> ServiceDescriptor:
        try:
            return self.pool.FindServiceByName(full_name)
        except KeyError:
            raise UnknownTypeError(f"no such service: '{full_name}'") from None

    def lookup(self, full_name: str) -> Union[Descriptor, EnumDescriptor, ServiceDescriptor]:
        """A message, enum or service by name."""
        for finder in (self.pool.FindMessageTypeByName, self.pool.FindEnumTypeByName, self.pool.FindServiceByName):
            try:
                return finder(full_name)
            except KeyError:
                continue
        raise UnknownTypeError(f"no such type, enum or service: '{full_name}'")

    def message_class(self, descriptor: Descriptor) -> Type[Message]:
        cls = self._classes.get(descriptor.full_name)
        if cls is None:
            cls = GetMessageClass(descriptor)
            self._classes[descriptor.full_name] = cls
        return cls


# ---------------------------------------------------------------------------
# Conversion between Python values and protobuf messages
# ---------------------------------------------------------------------------

_INT_TYPES = {
    FieldDescriptor.TYPE_INT32, FieldDescriptor.TYPE_INT64, FieldDescriptor.TYPE_UINT32,
    FieldDescriptor.TYPE_UINT64, FieldDescriptor.TYPE_SINT32, FieldDescriptor.TYPE_SINT64,
    FieldDescriptor.TYPE_FIXED32, FieldDescriptor.TYPE_FIXED64, FieldDescriptor.TYPE_SFIXED32,
    FieldDescriptor.TYPE_SFIXED64,
}
_FLOAT_TYPES = {FieldDescriptor.TYPE_DOUBLE, FieldDescriptor.TYPE_FLOAT}


def _custom_type_of(field: FieldDescriptor) -> Optional[CustomType]:
    """The custom type a field is declared as, if its type is a wrapper."""
    if field.type != FieldDescriptor.TYPE_MESSAGE or field.message_type is None:
        return None
    mt = field.message_type
    if mt.containing_type is not None or mt.file.package:
        return None
    if not mt.file.name.startswith("protobus/custom_types"):
        return None
    return get_custom_type(mt.name)


def _is_repeated(field: FieldDescriptor) -> bool:
    is_repeated = getattr(field, "is_repeated", None)
    if is_repeated is not None:
        return bool(is_repeated)
    return field.label == FieldDescriptor.LABEL_REPEATED  # protobuf < 6


def _is_map(field: FieldDescriptor) -> bool:
    return (
        field.type == FieldDescriptor.TYPE_MESSAGE
        and _is_repeated(field)
        and field.message_type is not None
        and field.message_type.GetOptions().map_entry
    )


class _ValueFree:
    """Marker: an encoding error whose message never carries the value."""


class FieldTypeError(_ValueFree, TypeError):
    """A field was given a value of the wrong type. Names the field and the
    type; never the value, because the message is logged."""


class FieldValueError(_ValueFree, ValueError):
    """A field was given a value of the right type that it cannot hold."""


def _coerce_scalar(field: FieldDescriptor, value: Any) -> Any:
    """Turn a Python value into what the protobuf message setter accepts.
    Every error raised here names the field and the value's TYPE, never the
    value: these messages are logged."""
    t = field.type
    if t in _INT_TYPES:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            raise FieldTypeError(f"field '{field.name}' expects an integer, got a non-integral float")
        if isinstance(value, str):
            try:
                return int(value.strip(), 0)
            except ValueError:
                raise FieldTypeError(f"field '{field.name}' expects an integer, got a str that is not one") from None
        raise FieldTypeError(f"field '{field.name}' expects an integer, got {type(value).__name__}")
    if t in _FLOAT_TYPES:
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                raise FieldTypeError(f"field '{field.name}' expects a number, got a str that is not one") from None
        raise FieldTypeError(f"field '{field.name}' expects a number, got {type(value).__name__}")
    if t == FieldDescriptor.TYPE_BOOL:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise FieldTypeError(f"field '{field.name}' expects a bool, got {type(value).__name__}")
    if t == FieldDescriptor.TYPE_STRING:
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            try:
                return bytes(value).decode("utf-8")
            except UnicodeDecodeError:
                raise FieldTypeError(f"field '{field.name}' expects a string, got bytes that are not UTF-8") from None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        raise FieldTypeError(f"field '{field.name}' expects a string, got {type(value).__name__}")
    if t == FieldDescriptor.TYPE_BYTES:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        if isinstance(value, str):
            # The canonical JSON mapping for bytes, and what a value that
            # crossed a JSON boundary looks like.
            try:
                return base64.b64decode(value)
            except (ValueError, TypeError):
                raise FieldTypeError(f"field '{field.name}' expects bytes, got a str that is not base64") from None
        raise FieldTypeError(f"field '{field.name}' expects bytes, got {type(value).__name__}")
    if t == FieldDescriptor.TYPE_ENUM:
        enum = field.enum_type
        if isinstance(value, bool):
            raise FieldTypeError(f"field '{field.name}' expects an enum value, got bool")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            ev = enum.values_by_name.get(value)
            if ev is None:
                if value.lstrip("-").isdigit():
                    return int(value)
                raise FieldValueError(f"field '{field.name}' got a str that is not a value of enum {enum.full_name}")
            return ev.number
        raise FieldTypeError(f"field '{field.name}' expects an enum value, got {type(value).__name__}")
    raise FieldTypeError(f"unsupported field type {t} on '{field.name}'")


def _fill_message(message: Message, obj: Any, root: Root) -> Message:
    """Populate ``message`` from a dict (or a Message of the same type)."""
    if isinstance(obj, Message):
        if obj.DESCRIPTOR.full_name != message.DESCRIPTOR.full_name:
            raise FieldTypeError(
                f"expected a {message.DESCRIPTOR.full_name}, got a {obj.DESCRIPTOR.full_name}"
            )
        message.CopyFrom(obj)
        return message
    if obj is None:
        return message
    if not isinstance(obj, dict):
        raise FieldTypeError(
            f"expected a dict for {message.DESCRIPTOR.full_name}, got {type(obj).__name__}"
        )

    descriptor = message.DESCRIPTOR
    for key, value in obj.items():
        field = descriptor.fields_by_name.get(key)
        if field is None:
            # Unknown keys are ignored, as protobufjs's create() ignores them:
            # a stale key in a caller's payload is dropped, not an outage.
            continue
        if value is None:
            continue
        try:
            _fill_field(message, field, value, root)
        except (FieldTypeError, FieldValueError):
            raise
        except Exception as err:
            # Whatever raised below — a custom codec, protobuf's own setter,
            # a decoder — may have put the offending VALUE in its message.
            # That text is for the caller, as __cause__; what protobus
            # raises and logs names the field and the type, never the value.
            raise FieldValueError(
                f"field '{field.name}' of {descriptor.full_name} rejected a {type(value).__name__} ({type(err).__name__})"
            ) from err
    return message


def _fill_field(message: Message, field: FieldDescriptor, value: Any, root: Root) -> None:
    for_error = message.DESCRIPTOR.full_name
    if True:
        custom = _custom_type_of(field)

        if _is_map(field):
            if not isinstance(value, dict):
                raise FieldTypeError(f"field '{field.name}' expects a dict, got {type(value).__name__}")
            target = getattr(message, field.name)
            value_field = field.message_type.fields_by_name["value"]
            value_custom = _custom_type_of(value_field)
            for k, v in value.items():
                if v is None:
                    return
                if value_custom is not None:
                    target[k].value = _wire_value(value_custom, v)
                elif value_field.type == FieldDescriptor.TYPE_MESSAGE:
                    _fill_message(target[k], v, root)
                else:
                    target[k] = _coerce_scalar(value_field, v)
            return

        if _is_repeated(field):
            if isinstance(value, (str, bytes, dict)) or not isinstance(value, Iterable):
                raise FieldTypeError(f"field '{field.name}' expects a list, got {type(value).__name__}")
            target = getattr(message, field.name)
            for item in value:
                if custom is not None:
                    target.add().value = _wire_value(custom, item)
                elif field.type == FieldDescriptor.TYPE_MESSAGE:
                    _fill_message(target.add(), item, root)
                else:
                    target.append(_coerce_scalar(field, item))
            return

        if custom is not None:
            getattr(message, field.name).value = _wire_value(custom, value)
        elif field.type == FieldDescriptor.TYPE_MESSAGE:
            sub = getattr(message, field.name)
            _fill_message(sub, value, root)
            # An empty dict is still a present message: `{}` and "unset" are
            # different things on the wire and must stay different here.
            sub.SetInParent()
        else:
            setattr(message, field.name, _coerce_scalar(field, value))


def _wire_value(custom: CustomType, value: Any) -> Any:
    """
    Encode a custom-type value, accepting one already in wire form: a dict
    ``{'value': <wire>}`` or, for bytes wire types, raw bytes.
    """
    if isinstance(value, dict) and set(value.keys()) == {"value"}:
        inner = value["value"]
        if custom.wire_type == "bytes" and isinstance(inner, (bytes, bytearray)):
            return bytes(inner)
        return custom.encode(inner)
    if custom.wire_type == "bytes" and isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return custom.encode(value)


def _to_python(message: Message, custom_decode: bool = True) -> Dict[str, Any]:
    """
    Convert a message to a dict, materialising proto3 defaults.

    Mirrors protobufjs's ``toObject(msg, {defaults: true, arrays: true,
    enums: String})``: every scalar is present (with its default if unset),
    every repeated field is a list, every map a dict, and an unset singular
    message is ``None``. A oneof member, including a proto3 ``optional``
    field, is present only when set — presence is the point of those.
    """
    out: Dict[str, Any] = {}
    descriptor = message.DESCRIPTOR
    for field in descriptor.fields:
        custom = _custom_type_of(field) if custom_decode else None

        if _is_map(field):
            value_field = field.message_type.fields_by_name["value"]
            value_custom = _custom_type_of(value_field) if custom_decode else None
            source = getattr(message, field.name)
            converted: Dict[Any, Any] = {}
            for k in source:
                v = source[k]
                if value_custom is not None:
                    converted[k] = value_custom.decode(v.value)
                elif value_field.type == FieldDescriptor.TYPE_MESSAGE:
                    converted[k] = _to_python(v, custom_decode)
                else:
                    converted[k] = _scalar_to_python(value_field, v)
            out[field.name] = converted
            continue

        if _is_repeated(field):
            source = getattr(message, field.name)
            if custom is not None:
                out[field.name] = [custom.decode(item.value) for item in source]
            elif field.type == FieldDescriptor.TYPE_MESSAGE:
                out[field.name] = [_to_python(item, custom_decode) for item in source]
            else:
                out[field.name] = [_scalar_to_python(field, item) for item in source]
            continue

        if field.containing_oneof is not None:
            # Declared or synthetic (proto3 optional): present only when set.
            if not message.HasField(field.name):
                continue

        if field.type == FieldDescriptor.TYPE_MESSAGE:
            if not message.HasField(field.name):
                out[field.name] = None
            elif custom is not None:
                out[field.name] = custom.decode(getattr(message, field.name).value)
            else:
                out[field.name] = _to_python(getattr(message, field.name), custom_decode)
            continue

        out[field.name] = _scalar_to_python(field, getattr(message, field.name))
    return out


def _scalar_to_python(field: FieldDescriptor, value: Any) -> Any:
    if field.type == FieldDescriptor.TYPE_ENUM:
        ev = field.enum_type.values_by_number.get(value)
        return ev.name if ev is not None else value
    if field.type == FieldDescriptor.TYPE_BYTES:
        return bytes(value)
    return value


# ---------------------------------------------------------------------------
# Finding .proto files
# ---------------------------------------------------------------------------


def find_files(start_path: str, suffix: str = ".proto") -> List[str]:
    """
    Every file under ``start_path`` ending in ``suffix``, recursively.

    ``endswith``, not a substring test: ``indexOf('.proto')`` in an earlier
    port also matched ``notes.protocol.txt`` and ``schema.proto.bak``.
    """
    root = Path(start_path)
    if root.is_file():
        return [str(root)] if root.name.endswith(suffix) else []
    if not root.is_dir():
        raise FileNotFoundError(f"proto path not found: {start_path}")
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(suffix):
                found.append(os.path.join(dirpath, name))
    return found


_METHOD_SPLIT_CACHE: Dict[str, Tuple[str, str]] = {}


# ---------------------------------------------------------------------------
# MessageFactory
# ---------------------------------------------------------------------------


class MessageFactory:
    """
    Builds and decodes the wire envelope, and holds the schema it is encoded
    against. One per Context.
    """

    def __init__(self) -> None:
        self.root: Optional[Root] = None
        self._is_initialized = False
        # Custom types registered before init(), to add to the root when it exists.
        self._registered_types: Dict[str, CustomType] = {}
        # Schema texts already added, so re-registering the same .proto is a
        # no-op regardless of the service name it arrives under.
        self._parsed_schemas: Set[str] = set()
        self._method_cache: Dict[str, MethodDescriptor] = {}

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    # TS parity: isInitialized is a property there too.
    isInitialized = is_initialized

    # -- static helpers -----------------------------------------------------

    @staticmethod
    def split_method_name(full_name: Any) -> Tuple[str, str]:
        """
        Split ``<package>.<Service>.<method>`` into (service, method).

        Parsed from the RIGHT: the method is the final segment and the
        service is everything before it, so a multi-segment package works and
        a name cannot carry a fourth segment past the method.
        """
        if not isinstance(full_name, str):
            raise InvalidMethodNameError(
                f"'{full_name}' is not a fully-qualified method name (<package>.<Service>.<method>)"
            )
        i = full_name.rfind(".")
        if i <= 0 or i == len(full_name) - 1:
            raise InvalidMethodNameError(
                f"'{full_name}' is not a fully-qualified method name (<package>.<Service>.<method>)"
            )
        return full_name[:i], full_name[i + 1:]

    # -- initialisation -----------------------------------------------------

    def init(self, root_paths: Optional[Union[str, List[str]]] = None) -> None:
        """
        Create the root and load every ``.proto`` under ``root_paths``.

        Synchronous, as in the TypeScript port: nothing here touches the
        network. Files are added in dependency order — a file whose imports
        are not loaded yet is retried once they are — so directory layout and
        ``import`` lines need not agree.
        """
        if isinstance(root_paths, str):
            root_paths = [root_paths]
        root_paths = root_paths or []

        self.root = Root()
        self._parsed_schemas.clear()
        self._method_cache.clear()

        # Types registered before init() go into the fresh root.
        for custom in self._registered_types.values():
            self.root.add_custom_type(custom)

        file_names: List[str] = []
        for root_path in root_paths:
            file_names.extend(find_files(root_path))

        if file_names:
            Logger.info(f"loading {len(file_names)} proto file(s)")
            self._load_files(file_names, root_paths)

        self._is_initialized = True
        Logger.debug("message factory initialized")

    def _load_files(self, file_names: List[str], root_paths: List[str]) -> None:
        assert self.root is not None
        pending: List[Tuple[str, str, str]] = []
        for file_name in file_names:
            with open(file_name, "r", encoding="utf-8") as fh:
                text = fh.read()
            pending.append((file_name, _relative_name(file_name, root_paths), text))

        # Dependency order by retry: a file that refers to a type nothing has
        # declared yet is set aside until something declares it.
        while pending:
            progressed = False
            last_error: Optional[Exception] = None
            remaining: List[Tuple[str, str, str]] = []
            for file_name, rel_name, text in pending:
                try:
                    if text in self._parsed_schemas:
                        progressed = True
                        continue
                    self.root.add_proto(text, rel_name)
                    self._parsed_schemas.add(text)
                    progressed = True
                except ProtoParseError as err:
                    if "unknown type" in str(err) or "unknown message type" in str(err):
                        last_error = err
                        remaining.append((file_name, rel_name, text))
                    else:
                        raise
            pending = remaining
            if pending and not progressed:
                assert last_error is not None
                raise last_error

    # -- schema queries -----------------------------------------------------

    def has_type(self, full_name: str) -> bool:
        """True if the fully-qualified message type is in this factory's root."""
        return self.root is not None and self.root.has_type(full_name)

    def has_service(self, full_name: str) -> bool:
        """True if the fully-qualified service is in this factory's root."""
        return self.root is not None and self.root.has_service(full_name)

    def lookup_service(self, full_name: str) -> Optional[ServiceDescriptor]:
        """The service descriptor, or None. (1.x API.)"""
        if self.root is None:
            return None
        try:
            return self.root.lookup_service(full_name)
        except UnknownTypeError:
            return None

    def get_service_method_names(self, service_full_name: str) -> List[str]:
        """Method names declared by a service, in declaration order."""
        self._require_root()
        return [m.name for m in self.root.lookup_service(service_full_name).methods]  # type: ignore[union-attr]

    def _get_method_type(self, full_name: str) -> MethodDescriptor:
        cached = self._method_cache.get(full_name)
        if cached is not None:
            return cached
        service_name, method_name = MessageFactory.split_method_name(full_name)
        self._require_root()
        service = self.root.lookup_service(service_name)  # type: ignore[union-attr]
        method = service.methods_by_name.get(method_name)
        if method is None:
            raise UnknownMethodError(f"service '{service_name}' declares no method '{method_name}'")
        self._method_cache[full_name] = method
        return method

    def get_method(self, full_name: str) -> MethodDescriptor:
        """The MethodDescriptor for ``<package>.<Service>.<method>``."""
        return self._get_method_type(full_name)

    def is_streaming_method(self, full_name: str) -> bool:
        """
        True if the method is declared ``returns (stream X)``. A method
        missing from the schema is treated as unary, with a debug line saying
        so — a typo'd name silently degrading to unary is hard to diagnose.
        """
        try:
            return bool(self._get_method_type(full_name).server_streaming)
        except Exception as err:
            Logger.debug(f"is_streaming_method({full_name}): treating as unary ({err})")
            return False

    # -- custom types -------------------------------------------------------

    def register_type(self, custom_type: CustomType) -> descriptor_pb2.DescriptorProto:
        """
        Register a custom type and add it to this factory's root.

        The registration is process-wide (see ``custom_types``); only the
        addition to the root is per instance. Idempotent: re-registering a
        name refreshes its codec. A definition that disagrees about
        ``wire_type`` is refused with CustomTypeConflictError.
        """
        known = self._registered_types.get(custom_type.name)
        if known is not None or is_custom_type(custom_type.name):
            refresh_custom_type_codec(custom_type)
            self._registered_types[custom_type.name] = custom_type
            if self.root is not None and not self.root.has_type(custom_type.name):
                self.root.add_custom_type(custom_type)
            return wrapper_descriptor(custom_type)

        descriptor = register_custom_type(custom_type)
        self._registered_types[custom_type.name] = custom_type
        if self.root is not None:
            self.root.add_custom_type(custom_type)
        return descriptor

    # -- parsing ------------------------------------------------------------

    def parse(self, proto: str, module_name: Optional[str] = None) -> None:
        """
        Add a schema to the root.

        Idempotent: re-parsing a schema already present is a no-op. Keyed on
        the service name AND the schema text, because a service's runtime
        name need not be the name declared in its .proto and several
        instances can share one schema under distinct names. Conflicting
        definitions are still an error: identical text is a no-op, a
        different definition of the same type is not.
        """
        if not self._is_initialized or self.root is None:
            raise NotInitializedError(
                f"cannot parse schema{f' for {module_name}' if module_name else ''} before "
                "MessageFactory.init() has run: there is no root to parse into, and the "
                "schema would be silently discarded. Call Context.init() (or "
                "MessageFactory.init()) first."
            )
        if module_name and self.has_service(module_name):
            Logger.debug(f"schema for {module_name} already registered, skipping")
            return
        if proto in self._parsed_schemas:
            Logger.debug(f"schema text already registered{f' (as {module_name})' if module_name else ''}, skipping")
            return
        if not proto.strip():
            # An empty schema declares nothing; recording it keeps the call idempotent.
            self._parsed_schemas.add(proto)
            return

        file_name = _schema_file_name(proto, module_name)
        self.root.add_proto(proto, file_name)
        self._parsed_schemas.add(proto)
        self._method_cache.clear()

    # -- encode / decode ----------------------------------------------------

    def _require_root(self) -> None:
        if not self._is_initialized or self.root is None:
            raise NotInitializedError("message factory not initialized")

    def _encode(self, type_name: str, obj: Any) -> bytes:
        descriptor = self.root.lookup_type(type_name)  # type: ignore[union-attr]
        message = self.root.message_class(descriptor)()  # type: ignore[union-attr]
        _fill_message(message, obj, self.root)  # type: ignore[arg-type]
        return message.SerializeToString()

    def decode_message(self, message_type: str, data: bytes) -> Dict[str, Any]:
        """Decode ``data`` as ``message_type`` into a dict."""
        self._require_root()
        if not message_type:
            raise MessageTypeRequiredError("message type required")
        descriptor = self.root.lookup_type(message_type)  # type: ignore[union-attr]
        try:
            message = self.root.message_class(descriptor).FromString(bytes(data or b""))  # type: ignore[union-attr]
            return _to_python(message)
        except Exception:
            # No payload in the log line: bodies routinely carry credentials
            # and personal data. Type name and byte length only.
            Logger.error(f"error decoding message {message_type} ({len(data) if data else 0} bytes)")
            raise

    def build_request(self, method_full_name: str, obj: Any, actor: Optional[str] = None) -> bytes:
        """Encode a request envelope with ``obj`` as the method's request type."""
        self._require_root()
        method = self._get_method_type(method_full_name)
        message_type = method.input_type.full_name
        try:
            data = self._encode(message_type, obj)
        except Exception as err:
            Logger.error(f"error building request {message_type}: {err}")
            raise
        container = _RequestContainerMsg(method=method_full_name, actor=actor or "", data=data)
        return container.SerializeToString()

    def decode_request_envelope(self, data: bytes) -> RequestContainer:
        """
        Decode the envelope only, leaving ``data`` as the undecoded payload.

        Separate from the payload decode so a caller can check which method
        the envelope names BEFORE interpreting the bytes: decoding first means
        choosing the schema from a publisher-controlled field.
        """
        container = _RequestContainerMsg.FromString(bytes(data))
        return RequestContainer(method=container.method, actor=container.actor, data=bytes(container.data))

    def decode_request_payload(self, method_full_name: str, payload: bytes) -> Dict[str, Any]:
        """Decode a request payload against the declared request type."""
        method = self._get_method_type(method_full_name)
        return self.decode_message(method.input_type.full_name, payload)

    def decode_request(self, data: bytes) -> RequestContainer:
        envelope = self.decode_request_envelope(data)
        return RequestContainer(
            method=envelope.method,
            actor=envelope.actor,
            data=self.decode_request_payload(envelope.method, envelope.data),
        )

    def build_response(self, method_full_name: str, obj: Any) -> bytes:
        """
        Encode a response envelope. An exception becomes an error response
        carrying its message and ``code`` (``''`` when it has none) — with no
        method lookup, so a failure that is *about* an unknown method can
        still be reported.
        """
        self._require_root()
        if isinstance(obj, BaseException):
            container = _ResponseContainerMsg()
            container.error.method = method_full_name or ""
            container.error.message = _error_text(obj)
            container.error.code = str(getattr(obj, "code", "") or "")
            return container.SerializeToString()

        method = self._get_method_type(method_full_name)
        message_type = method.output_type.full_name
        try:
            data = self._encode(message_type, obj)
        except Exception as err:
            Logger.error(f"error building response {message_type}: {err}")
            raise
        container = _ResponseContainerMsg()
        container.result.method = method_full_name
        container.result.data = data
        return container.SerializeToString()

    def decode_response(self, data: bytes) -> ResponseContainer:
        container = _ResponseContainerMsg.FromString(bytes(data))
        if container.HasField("error"):
            err = container.error
            return ResponseContainer(error=ResponseError(method=err.method, message=err.message, code=err.code))
        if not container.HasField("result"):
            return ResponseContainer()
        result = container.result
        method = self._get_method_type(result.method)
        return ResponseContainer(
            result=ResponseResult(
                method=result.method,
                data=self.decode_message(method.output_type.full_name, result.data),
            )
        )

    def build_event(self, type_name: str, obj: Any, topic: str) -> bytes:
        """Encode an event envelope with ``obj`` as ``type_name``."""
        self._require_root()
        try:
            data = self._encode(type_name, obj)
        except Exception as err:
            Logger.error(f"failed building event message {type_name}: {err}")
            raise
        container = _EventContainerMsg(type=type_name, topic=topic or "", data=data)
        return container.SerializeToString()

    def decode_event(self, data: bytes) -> EventContainer:
        container = _EventContainerMsg.FromString(bytes(data))
        return EventContainer(
            type=container.type,
            topic=container.topic,
            data=self.decode_message(container.type, container.data),
        )

    # -- code generation ----------------------------------------------------

    def export_python(self, service_names: Union[str, List[str]]) -> str:
        """
        Generate Python typing for the given services: a ``TypedDict`` per
        message, a ``Literal`` per enum, and a ``Protocol`` per service whose
        method signatures match what ``ServiceProxy`` installs. The
        counterpart of the TypeScript port's ``exportTS()``.
        """
        from .cli.generate_types import export_python

        if isinstance(service_names, str):
            service_names = [service_names]
        self._require_root()
        return export_python(self.root, service_names)  # type: ignore[arg-type]

    # TS parity alias.
    exportTS = export_python


def _error_text(error: BaseException) -> str:
    text = getattr(error, "message", None)
    if isinstance(text, str) and text:
        return text
    return str(error) or type(error).__name__


_SAFE_FILE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _schema_file_name(proto: str, module_name: Optional[str]) -> str:
    digest = hashlib.sha1(proto.encode("utf-8")).hexdigest()[:12]
    if module_name:
        return f"protobus/{_SAFE_FILE_NAME.sub('_', module_name)}.{digest}.proto"
    return f"protobus/{digest}.proto"


def _relative_name(file_name: str, root_paths: List[str]) -> str:
    """A file's name as an ``import`` would spell it: relative to its root."""
    path = Path(file_name).resolve()
    for root in root_paths:
        try:
            return path.relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            continue
    return path.name
