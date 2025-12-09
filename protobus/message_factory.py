"""Message factory for Protocol Buffer encoding/decoding with custom type support."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.compiler import plugin_pb2
from google.protobuf.descriptor import Descriptor, FieldDescriptor, ServiceDescriptor
from google.protobuf.message import Message

from .custom_types import CustomType, get_custom_type, is_custom_type, register_custom_type
from .errors import MessageTypeRequiredError, NotInitializedError
from .logger import Logger


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


class MessageFactory:
    """
    Factory for building, parsing, and managing protobuf messages.

    Handles Protocol Buffer encoding/decoding with support for custom types.
    """

    def __init__(self):
        self._pool: Optional[descriptor_pool.DescriptorPool] = None
        self._factory: Optional[message_factory.MessageFactory] = None
        self._message_classes: Dict[str, Type[Message]] = {}
        self._service_descriptors: Dict[str, ServiceDescriptor] = {}
        self._initialized = False
        self._proto_sources: Dict[str, str] = {}

    @property
    def root(self) -> "MessageFactory":
        """Return self for compatibility with TypeScript API."""
        return self

    @property
    def is_initialized(self) -> bool:
        """Check if the factory has been initialized."""
        return self._initialized

    async def init(self, root_paths: Optional[List[str]] = None) -> None:
        """
        Initialize the message factory by loading .proto files.

        Args:
            root_paths: List of directories to search for .proto files
        """
        self._pool = descriptor_pool.DescriptorPool()
        self._factory = message_factory.MessageFactory(pool=self._pool)
        self._initialized = True
        Logger.debug("MessageFactory initialized")

    def parse(self, proto_source: str, service_name: str) -> None:
        """
        Parse a proto source string and register its types.

        This is a simplified implementation that stores the proto source
        for later use. In production, you would use protoc to compile
        the proto files to Python modules.

        Args:
            proto_source: Proto file content as string
            service_name: Name of the service defined in the proto
        """
        if not self._initialized:
            raise NotInitializedError("MessageFactory not initialized")

        self._proto_sources[service_name] = proto_source
        Logger.debug(f"Registered proto source for service: {service_name}")

    def register_type(self, custom_type: CustomType) -> None:
        """
        Register a custom type for encoding/decoding.

        Args:
            custom_type: The custom type definition
        """
        register_custom_type(custom_type)
        Logger.debug(f"Registered custom type: {custom_type.name}")

    def lookup_service(self, name: str) -> Optional[ServiceDescriptor]:
        """
        Look up a service descriptor by name.

        Args:
            name: Fully qualified service name

        Returns:
            ServiceDescriptor or None if not found
        """
        return self._service_descriptors.get(name)

    def _preprocess_for_encode(self, data: Any, type_name: Optional[str] = None) -> Any:
        """
        Preprocess data before encoding, handling custom types.

        Args:
            data: Data to preprocess
            type_name: Optional type name hint

        Returns:
            Preprocessed data
        """
        if data is None:
            return data

        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                # Check if the value corresponds to a custom type
                if is_custom_type(key):
                    custom_type = get_custom_type(key)
                    if custom_type:
                        result[key] = custom_type.encode(value)
                else:
                    result[key] = self._preprocess_for_encode(value)
            return result

        if isinstance(data, list):
            return [self._preprocess_for_encode(item) for item in data]

        return data

    def _postprocess_after_decode(self, data: Any, type_name: Optional[str] = None) -> Any:
        """
        Postprocess data after decoding, handling custom types.

        Args:
            data: Data to postprocess
            type_name: Optional type name hint

        Returns:
            Postprocessed data
        """
        if data is None:
            return data

        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                if is_custom_type(key):
                    custom_type = get_custom_type(key)
                    if custom_type:
                        result[key] = custom_type.decode(value)
                else:
                    result[key] = self._postprocess_after_decode(value)
            return result

        if isinstance(data, list):
            return [self._postprocess_after_decode(item) for item in data]

        return data

    def build_request(
        self, method: str, data: Any, actor: Optional[str] = None
    ) -> bytes:
        """
        Build a request message.

        Args:
            method: Full method name (package.service.method)
            data: Request data
            actor: Optional actor identifier

        Returns:
            Encoded message bytes
        """
        if not self._initialized:
            raise NotInitializedError("MessageFactory not initialized")

        # Create a simple envelope format
        # In production, this would use the actual proto message types
        import json

        envelope = {
            "method": method,
            "data": self._preprocess_for_encode(data),
            "actor": actor,
        }
        return json.dumps(envelope).encode("utf-8")

    def build_response(
        self, method: str, result: Any = None, error: Optional[Exception] = None
    ) -> bytes:
        """
        Build a response message.

        Args:
            method: Full method name
            result: Response result (or error if isinstance Exception)
            error: Optional error

        Returns:
            Encoded message bytes
        """
        if not self._initialized:
            raise NotInitializedError("MessageFactory not initialized")

        import json

        # Handle case where result is an exception
        if isinstance(result, Exception):
            error = result
            result = None

        envelope: Dict[str, Any] = {"method": method}

        if error is not None:
            envelope["error"] = {
                "message": str(error),
                "code": getattr(error, "code", "UNKNOWN_ERROR"),
            }
        else:
            envelope["result"] = {"data": self._preprocess_for_encode(result)}

        return json.dumps(envelope).encode("utf-8")

    def build_event(
        self, event_type: str, data: Any, topic: Optional[str] = None
    ) -> bytes:
        """
        Build an event message.

        Args:
            event_type: Event type name
            data: Event data
            topic: Optional topic string

        Returns:
            Encoded message bytes
        """
        if not self._initialized:
            raise NotInitializedError("MessageFactory not initialized")

        import json

        envelope = {
            "type": event_type,
            "data": self._preprocess_for_encode(data),
            "topic": topic,
        }
        return json.dumps(envelope).encode("utf-8")

    def decode_message(self, data: bytes) -> Dict[str, Any]:
        """
        Decode a raw message.

        Args:
            data: Encoded message bytes

        Returns:
            Decoded message dictionary
        """
        import json

        return json.loads(data.decode("utf-8"))

    def decode_request(self, data: bytes) -> RequestContainer:
        """
        Decode a request message.

        Args:
            data: Encoded request bytes

        Returns:
            RequestContainer with decoded data
        """
        decoded = self.decode_message(data)
        return RequestContainer(
            method=decoded.get("method", ""),
            data=self._postprocess_after_decode(decoded.get("data")),
            actor=decoded.get("actor"),
        )

    def decode_response(self, data: bytes) -> ResponseContainer:
        """
        Decode a response message.

        Args:
            data: Encoded response bytes

        Returns:
            ResponseContainer with decoded data
        """
        decoded = self.decode_message(data)

        error = None
        if "error" in decoded:
            error = decoded["error"]

        result = None
        if "result" in decoded:
            result = self._postprocess_after_decode(decoded["result"])

        return ResponseContainer(
            method=decoded.get("method", ""),
            result=result,
            error=error,
        )

    def decode_event(self, data: bytes) -> EventContainer:
        """
        Decode an event message.

        Args:
            data: Encoded event bytes

        Returns:
            EventContainer with decoded data
        """
        decoded = self.decode_message(data)
        return EventContainer(
            type=decoded.get("type", ""),
            data=self._postprocess_after_decode(decoded.get("data")),
            topic=decoded.get("topic"),
        )

    def export_python(self, service_names: List[str]) -> str:
        """
        Generate Python type definitions from service definitions.

        Args:
            service_names: List of service names to export

        Returns:
            Python code string with type definitions
        """
        # This is a placeholder for TypeScript-like interface generation
        # In Python, we would typically use dataclasses or TypedDict
        lines = [
            "# Auto-generated type definitions",
            "from typing import Any, Optional",
            "from dataclasses import dataclass",
            "",
        ]

        for service_name in service_names:
            lines.append(f"# Types for {service_name}")
            lines.append(f"# (Proto source available but not compiled)")
            lines.append("")

        return "\n".join(lines)
