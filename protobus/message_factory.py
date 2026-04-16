"""Message factory for Protocol Buffer binary encoding/decoding.

Compatible with the TypeScript protobus implementation which uses
protobufjs binary wire format for all messages.

Wire format:
  RequestContainer  { method: string, actor: string, data: bytes }
  ResponseContainer { result: ResponseResult, error: ResponseError }
  ResponseResult    { method: string, data: bytes }
  ResponseError     { method: string, message: string, code: string }
  EventContainer    { type: string, topic: string, data: bytes }

The inner `data` bytes field contains the service-specific message
(e.g., ChatRequest) encoded as protobuf binary using the .proto
definitions loaded at init time.
"""

import json
import os
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

from google.protobuf import descriptor_pb2, descriptor_pool, symbol_database
from google.protobuf import json_format
from google.protobuf.descriptor import Descriptor, FieldDescriptor, ServiceDescriptor
from google.protobuf.message import Message
from google.protobuf.message_factory import GetMessageClass

from .custom_types import CustomType, get_custom_type, is_custom_type, register_custom_type
from .errors import MessageTypeRequiredError, NotInitializedError
from .logger import Logger

# ---------------------------------------------------------------------------
# Internal container .proto (matches the TS side exactly)
# ---------------------------------------------------------------------------

_CONTAINER_PROTO = """
syntax = "proto3";

message RequestContainer {
  string method = 1;
  string actor = 2;
  bytes data = 3;
}

message ResponseResult {
  string method = 1;
  bytes data = 2;
}

message ResponseError {
  string method = 1;
  string message = 2;
  string code = 3;
}

message ResponseContainer {
  ResponseResult result = 1;
  ResponseError error = 2;
}

message EventContainer {
  string type = 1;
  string topic = 2;
  bytes data = 3;
}
"""


@dataclass
class RequestContainer:
    """Container for decoded request messages."""

    method: str
    data: Any
    actor: Optional[str] = None


@dataclass
class ResponseContainer:
    """Container for decoded response messages."""

    method: str
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None


@dataclass
class EventContainer:
    """Container for decoded event messages."""

    type: str
    data: Any
    topic: Optional[str] = None


# ---------------------------------------------------------------------------
# Compiled container message classes (built once at module load)
# ---------------------------------------------------------------------------

def _build_container_classes():
    """Compile the container proto and return message classes."""
    from google.protobuf import descriptor_pb2 as dp
    from google.protobuf.descriptor import FileDescriptor, MakeDescriptor

    # We use a minimal approach: define the descriptors manually
    # so we don't need protoc at runtime.

    pool = descriptor_pool.DescriptorPool()

    # Build FileDescriptorProto from the container schema
    file_proto = dp.FileDescriptorProto()
    file_proto.name = "protobus_containers.proto"
    file_proto.syntax = "proto3"

    # RequestContainer
    msg = file_proto.message_type.add()
    msg.name = "RequestContainer"
    f = msg.field.add()
    f.name = "method"; f.number = 1; f.type = FieldDescriptor.TYPE_STRING; f.label = FieldDescriptor.LABEL_OPTIONAL
    f = msg.field.add()
    f.name = "actor"; f.number = 2; f.type = FieldDescriptor.TYPE_STRING; f.label = FieldDescriptor.LABEL_OPTIONAL
    f = msg.field.add()
    f.name = "data"; f.number = 3; f.type = FieldDescriptor.TYPE_BYTES; f.label = FieldDescriptor.LABEL_OPTIONAL

    # ResponseResult
    msg = file_proto.message_type.add()
    msg.name = "ResponseResult"
    f = msg.field.add()
    f.name = "method"; f.number = 1; f.type = FieldDescriptor.TYPE_STRING; f.label = FieldDescriptor.LABEL_OPTIONAL
    f = msg.field.add()
    f.name = "data"; f.number = 2; f.type = FieldDescriptor.TYPE_BYTES; f.label = FieldDescriptor.LABEL_OPTIONAL

    # ResponseError
    msg = file_proto.message_type.add()
    msg.name = "ResponseError"
    f = msg.field.add()
    f.name = "method"; f.number = 1; f.type = FieldDescriptor.TYPE_STRING; f.label = FieldDescriptor.LABEL_OPTIONAL
    f = msg.field.add()
    f.name = "message"; f.number = 2; f.type = FieldDescriptor.TYPE_STRING; f.label = FieldDescriptor.LABEL_OPTIONAL
    f = msg.field.add()
    f.name = "code"; f.number = 3; f.type = FieldDescriptor.TYPE_STRING; f.label = FieldDescriptor.LABEL_OPTIONAL

    # ResponseContainer
    msg = file_proto.message_type.add()
    msg.name = "ResponseContainer"
    f = msg.field.add()
    f.name = "result"; f.number = 1; f.type = FieldDescriptor.TYPE_MESSAGE; f.label = FieldDescriptor.LABEL_OPTIONAL
    f.type_name = "ResponseResult"
    f = msg.field.add()
    f.name = "error"; f.number = 2; f.type = FieldDescriptor.TYPE_MESSAGE; f.label = FieldDescriptor.LABEL_OPTIONAL
    f.type_name = "ResponseError"

    # EventContainer
    msg = file_proto.message_type.add()
    msg.name = "EventContainer"
    f = msg.field.add()
    f.name = "type"; f.number = 1; f.type = FieldDescriptor.TYPE_STRING; f.label = FieldDescriptor.LABEL_OPTIONAL
    f = msg.field.add()
    f.name = "topic"; f.number = 2; f.type = FieldDescriptor.TYPE_STRING; f.label = FieldDescriptor.LABEL_OPTIONAL
    f = msg.field.add()
    f.name = "data"; f.number = 3; f.type = FieldDescriptor.TYPE_BYTES; f.label = FieldDescriptor.LABEL_OPTIONAL

    pool.Add(file_proto)

    classes = {}
    for name in ["RequestContainer", "ResponseResult", "ResponseError", "ResponseContainer", "EventContainer"]:
        desc = pool.FindMessageTypeByName(name)
        classes[name] = GetMessageClass(desc)

    return classes


_CONTAINER_CLASSES = _build_container_classes()
_RequestContainerMsg = _CONTAINER_CLASSES["RequestContainer"]
_ResponseContainerMsg = _CONTAINER_CLASSES["ResponseContainer"]
_ResponseResultMsg = _CONTAINER_CLASSES["ResponseResult"]
_ResponseErrorMsg = _CONTAINER_CLASSES["ResponseError"]
_EventContainerMsg = _CONTAINER_CLASSES["EventContainer"]


# ---------------------------------------------------------------------------
# Service proto pool — for decoding inner data messages
# ---------------------------------------------------------------------------

def _load_proto_files(proto_dirs: List[str]) -> descriptor_pool.DescriptorPool:
    """Load .proto files from directories into a descriptor pool."""
    import subprocess
    import tempfile

    pool = descriptor_pool.DescriptorPool()

    proto_files = []
    for d in proto_dirs:
        p = Path(d)
        if p.is_dir():
            proto_files.extend(p.glob("*.proto"))
        elif p.is_file() and p.suffix == ".proto":
            proto_files.append(p)

    if not proto_files:
        Logger.debug("No .proto files found")
        return pool

    # Use protoc to compile .proto files to FileDescriptorSet
    proto_paths = set()
    for f in proto_files:
        proto_paths.add(str(f.parent))

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        cmd = ["protoc"]
        for pp in proto_paths:
            cmd.extend(["-I", pp])
        cmd.extend(["--descriptor_set_out", tmp_path])
        cmd.extend([str(f) for f in proto_files])

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            Logger.warn(f"protoc failed: {result.stderr}. Falling back to JSON mode.")
            return pool

        with open(tmp_path, "rb") as fh:
            fds = descriptor_pb2.FileDescriptorSet()
            fds.ParseFromString(fh.read())

        for file_proto in fds.file:
            try:
                pool.Add(file_proto)
            except Exception:
                pass  # already registered

    except FileNotFoundError:
        Logger.warn("protoc not found. Inner messages will be decoded as JSON fallback.")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return pool


class MessageFactory:
    """
    Factory for building, parsing, and managing protobuf messages.

    Uses Protocol Buffer binary encoding compatible with the TypeScript
    protobus implementation. Falls back to JSON for inner data messages
    when proto definitions are not available.
    """

    def __init__(self):
        self._service_pool: Optional[descriptor_pool.DescriptorPool] = None
        self._service_msg_classes: Dict[str, Type[Message]] = {}
        self._initialized = False
        self._proto_sources: Dict[str, str] = {}
        self._proto_dirs: List[str] = []
        # Cache for method -> (request_type_name, response_type_name)
        self._method_types: Dict[str, tuple] = {}
        # Cache for checking if message types contain custom types
        self._has_custom_types_cache: Dict[str, bool] = {}

    @property
    def root(self) -> "MessageFactory":
        return self

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def init(self, root_paths: Optional[List[str]] = None) -> None:
        """Initialize the message factory, optionally loading .proto files."""
        self._proto_dirs = root_paths or []
        if self._proto_dirs:
            self._service_pool = _load_proto_files(self._proto_dirs)
            self._discover_services()
        else:
            self._service_pool = descriptor_pool.DescriptorPool()
        self._initialized = True
        Logger.debug("MessageFactory initialized")

    def _discover_services(self):
        """Discover service methods and their request/response types from the pool."""
        if not self._service_pool:
            return
        # We can't easily iterate a DescriptorPool, so we rely on
        # parse() calls to register service method types lazily.

    def parse(self, proto_source: str, service_name: str) -> None:
        """Register a proto source for a service."""
        if not self._initialized:
            raise NotInitializedError("MessageFactory not initialized")
        self._proto_sources[service_name] = proto_source
        Logger.debug(f"Registered proto source for service: {service_name}")

    def _get_inner_message_class(self, type_name: str) -> Optional[Type[Message]]:
        """Get a compiled message class for an inner data type."""
        if type_name in self._service_msg_classes:
            return self._service_msg_classes[type_name]

        if not self._service_pool:
            return None

        try:
            desc = self._service_pool.FindMessageTypeByName(type_name)
            cls = GetMessageClass(desc)
            self._service_msg_classes[type_name] = cls
            return cls
        except KeyError:
            return None

    def _resolve_method_types(self, method: str) -> Optional[tuple]:
        """Resolve method name to (request_type, response_type) names."""
        if method in self._method_types:
            return self._method_types[method]

        if not self._service_pool:
            return None

        # method format: "Package.Service.methodName"
        parts = method.rsplit(".", 1)
        if len(parts) != 2:
            return None

        service_full = parts[0]  # e.g. "ChatAgent.Service"
        method_name = parts[1]   # e.g. "chat"

        try:
            svc_desc = self._service_pool.FindServiceByName(service_full)
            for m in svc_desc.methods:
                if m.name == method_name:
                    req_type = m.input_type.full_name
                    res_type = m.output_type.full_name
                    self._method_types[method] = (req_type, res_type)
                    return (req_type, res_type)
        except KeyError:
            pass

        return None

    def _decode_inner_data(self, data_bytes: bytes, type_name: Optional[str] = None) -> Any:
        """Decode inner data bytes. Uses protobuf if type is known, else JSON fallback."""
        if not data_bytes:
            return {}

        # Try protobuf decode if we have the type
        if type_name:
            cls = self._get_inner_message_class(type_name)
            if cls:
                try:
                    msg = cls()
                    msg.ParseFromString(data_bytes)
                    return json_format.MessageToDict(msg, preserving_proto_field_name=True)
                except Exception as e:
                    Logger.debug(f"Protobuf decode failed for {type_name}: {e}, trying JSON")

        # JSON fallback
        try:
            return json.loads(data_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            Logger.warn(f"Cannot decode inner data as JSON or protobuf")
            return {}

    def _encode_inner_data(self, data: Any, type_name: Optional[str] = None) -> bytes:
        """Encode inner data to bytes. Uses protobuf if type is known, else JSON."""
        if data is None:
            return b""

        # Preprocess custom types
        data = self._preprocess_for_encode(data)

        # Try protobuf encode if we have the type
        if type_name:
            cls = self._get_inner_message_class(type_name)
            if cls:
                try:
                    msg = json_format.ParseDict(data, cls())
                    return msg.SerializeToString()
                except Exception as e:
                    Logger.debug(f"Protobuf encode failed for {type_name}: {e}, using JSON")

        # JSON fallback
        return json.dumps(data).encode("utf-8")

    # ── Build methods ──────────────────────────────────────────────

    def build_request(self, method: str, data: Any, actor: Optional[str] = None) -> bytes:
        """Build a binary protobuf request message."""
        if not self._initialized:
            raise NotInitializedError("MessageFactory not initialized")

        types = self._resolve_method_types(method)
        req_type = types[0] if types else None
        inner_bytes = self._encode_inner_data(data, req_type)

        container = _RequestContainerMsg()
        container.method = method
        container.actor = actor or ""
        container.data = inner_bytes

        return container.SerializeToString()

    def build_response(self, method: str, result: Any = None, error: Optional[Exception] = None) -> bytes:
        """Build a binary protobuf response message."""
        if not self._initialized:
            raise NotInitializedError("MessageFactory not initialized")

        container = _ResponseContainerMsg()

        if isinstance(result, Exception):
            error = result
            result = None

        if error is not None:
            err = _ResponseErrorMsg()
            err.method = method
            err.message = str(error)
            err.code = getattr(error, "code", "UNKNOWN_ERROR")
            container.error.CopyFrom(err)
        else:
            types = self._resolve_method_types(method)
            res_type = types[1] if types else None
            inner_bytes = self._encode_inner_data(result, res_type)

            res = _ResponseResultMsg()
            res.method = method
            res.data = inner_bytes
            container.result.CopyFrom(res)

        return container.SerializeToString()

    def build_event(self, event_type: str, data: Any, topic: Optional[str] = None) -> bytes:
        """Build a binary protobuf event message."""
        if not self._initialized:
            raise NotInitializedError("MessageFactory not initialized")

        inner_bytes = self._encode_inner_data(data)

        container = _EventContainerMsg()
        container.type = event_type
        container.topic = topic or ""
        container.data = inner_bytes

        return container.SerializeToString()

    # ── Decode methods ─────────────────────────────────────────────

    def decode_request(self, data: bytes) -> "RequestContainer":
        """Decode a binary protobuf request message."""
        container = _RequestContainerMsg()
        container.ParseFromString(data)

        method = container.method
        types = self._resolve_method_types(method)
        req_type = types[0] if types else None

        decoded_data = self._decode_inner_data(container.data, req_type)
        decoded_data = self._postprocess_after_decode(decoded_data)

        return RequestContainer(
            method=method,
            data=decoded_data,
            actor=container.actor or None,
        )

    def decode_response(self, data: bytes) -> "ResponseContainer":
        """Decode a binary protobuf response message."""
        container = _ResponseContainerMsg()
        container.ParseFromString(data)

        if container.HasField("error") and container.error.method:
            return ResponseContainer(
                method=container.error.method,
                error={
                    "message": container.error.message,
                    "code": container.error.code,
                },
            )

        result_msg = container.result
        method = result_msg.method

        types = self._resolve_method_types(method)
        res_type = types[1] if types else None

        decoded_data = self._decode_inner_data(result_msg.data, res_type)
        decoded_data = self._postprocess_after_decode(decoded_data)

        return ResponseContainer(
            method=method,
            result=decoded_data,
        )

    def decode_event(self, data: bytes) -> "EventContainer":
        """Decode a binary protobuf event message."""
        container = _EventContainerMsg()
        container.ParseFromString(data)

        decoded_data = self._decode_inner_data(container.data)
        decoded_data = self._postprocess_after_decode(decoded_data)

        return EventContainer(
            type=container.type,
            data=decoded_data,
            topic=container.topic or None,
        )

    # Legacy alias
    def decode_message(self, data: bytes) -> Dict[str, Any]:
        """Decode raw bytes — tries request container first, then JSON fallback."""
        try:
            req = self.decode_request(data)
            return {"method": req.method, "data": req.data, "actor": req.actor}
        except Exception:
            pass
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {}

    # ── Custom type support ────────────────────────────────────────

    def register_type(self, custom_type: CustomType) -> None:
        register_custom_type(custom_type)
        Logger.debug(f"Registered custom type: {custom_type.name}")

    def lookup_service(self, name: str) -> Optional[ServiceDescriptor]:
        if not self._service_pool:
            return None
        try:
            return self._service_pool.FindServiceByName(name)
        except KeyError:
            return None

    def _preprocess_for_encode(self, data: Any) -> Any:
        if data is None:
            return data
        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                if is_custom_type(key):
                    ct = get_custom_type(key)
                    result[key] = ct.encode(value) if ct else value
                else:
                    result[key] = self._preprocess_for_encode(value)
            return result
        if isinstance(data, list):
            return [self._preprocess_for_encode(item) for item in data]
        return data

    def _postprocess_after_decode(self, data: Any) -> Any:
        if data is None:
            return data
        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                if is_custom_type(key):
                    ct = get_custom_type(key)
                    result[key] = ct.decode(value) if ct else value
                else:
                    result[key] = self._postprocess_after_decode(value)
            return result
        if isinstance(data, list):
            return [self._postprocess_after_decode(item) for item in data]
        return data

    def export_python(self, service_names: List[str]) -> str:
        lines = [
            "# Auto-generated type definitions",
            "from typing import Any, Optional",
            "from dataclasses import dataclass",
            "",
        ]
        for service_name in service_names:
            lines.append(f"# Types for {service_name}")
            lines.append("")
        return "\n".join(lines)
