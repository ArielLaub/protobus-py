"""
Protobus - A lightweight, scalable microservices message bus.

Leverages RabbitMQ for message routing and load balancing, combined with
Protocol Buffers for efficient, type-safe serialization.

Unlike transport-agnostic frameworks, Protobus embraces RabbitMQ's native
capabilities directly - topic exchanges, routing keys, competing consumers,
dead-letter queues, and message persistence.
"""

# Context and connection
from .context import Context, ContextOptions, IContext
from .connection import Connection, ConnectionOptions, IConnection, RetryOptions

# Message factory
from .message_factory import (
    MessageFactory,
    RequestContainer,
    ResponseContainer,
    EventContainer,
)

# Services
from .message_service import MessageService, MessageServiceOptions
from .runnable_service import RunnableService
from .proxied_service import ProxiedService
from .service_proxy import ServiceProxy
from .service_cluster import ServiceCluster

# Event handling
from .event_listener import EventListener, EventHandler

# Errors
from .errors import (
    HandledError,
    is_handled_error,
    AlreadyConnectedError,
    TimeoutError,
    ReconnectionError,
    DisconnectedError,
    NotConnectedError,
    NotInitializedError,
    AlreadyInitializedError,
    AlreadyStartedError,
    MessageTypeRequiredError,
    InvalidMessageError,
    InvalidRequestError,
    InvalidResponseError,
    InvalidServiceNameError,
    InvalidMethodError,
    InvalidResultError,
    PublishMessageError,
    MissingProtoError,
    MissingExchangeError,
    ConnectionError,
)

# Custom types
from .custom_types import (
    CustomType,
    register_custom_type,
    get_custom_type,
    is_custom_type,
    get_custom_type_names,
    bigint_to_bytes,
    bytes_to_bigint,
    BigIntType,
    TimestampType,
)

# Logger
from .logger import Logger, ILogger, set_logger

# Config
from .config import Config

__version__ = "1.2.1"

__all__ = [
    # Context and connection
    "Context",
    "ContextOptions",
    "IContext",
    "Connection",
    "ConnectionOptions",
    "IConnection",
    "RetryOptions",
    # Message factory
    "MessageFactory",
    "RequestContainer",
    "ResponseContainer",
    "EventContainer",
    # Services
    "MessageService",
    "MessageServiceOptions",
    "RunnableService",
    "ProxiedService",
    "ServiceProxy",
    "ServiceCluster",
    # Event handling
    "EventListener",
    "EventHandler",
    # Errors
    "HandledError",
    "is_handled_error",
    "AlreadyConnectedError",
    "TimeoutError",
    "ReconnectionError",
    "DisconnectedError",
    "NotConnectedError",
    "NotInitializedError",
    "AlreadyInitializedError",
    "AlreadyStartedError",
    "MessageTypeRequiredError",
    "InvalidMessageError",
    "InvalidRequestError",
    "InvalidResponseError",
    "InvalidServiceNameError",
    "InvalidMethodError",
    "InvalidResultError",
    "PublishMessageError",
    "MissingProtoError",
    "MissingExchangeError",
    "ConnectionError",
    # Custom types
    "CustomType",
    "register_custom_type",
    "get_custom_type",
    "is_custom_type",
    "get_custom_type_names",
    "bigint_to_bytes",
    "bytes_to_bigint",
    "BigIntType",
    "TimestampType",
    # Logger
    "Logger",
    "ILogger",
    "set_logger",
    # Config
    "Config",
]
