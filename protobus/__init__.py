"""
Protobus — a lightweight, scalable micro-services message bus.

RabbitMQ for routing and load balancing, Protocol Buffers for the wire.
This is the Python port of the TypeScript `protobus`_ library; the two are
wire-compatible and share one behavioural contract.

.. _protobus: https://github.com/ArielLaub/protobus
"""

# Context
from .context import Context, ContextOptions, IContext

# Services
from .message_service import (
    DEFAULT_RETRY_OPTIONS,
    IMessageServiceOptions,
    MessageService,
    MessageServiceOptions,
)
from .message_listener import RetryOptions, RetryConfig
from .proxied_service import ProxiedService
from .service_proxy import RemoteError, ServiceProxy, StreamingCall
from .runnable_service import RunnableService
from .service_cluster import ServiceCluster

# Events
from .event_listener import EventHandler, EventListener, EventRetryOptions

# Connection
from .connection import (
    Connection,
    ConsumeOptions,
    ConsumeRetryOptions,
    IConnection,
    MessageHandler,
    MessageHandlerContext,
    MessageHandlerResult,
    ReconnectionOptions,
    Restorer,
    apply_heartbeat,
    attach_restorer,
)
from .cancellation import AbortController, AbortSignal

# Dispatchers
from .message_dispatcher import CallOptions, StreamOptions, StreamingReply

# Factory
from .message_factory import (
    EventContainer,
    MessageFactory,
    RequestContainer,
    ResponseContainer,
    ResponseError,
    ResponseResult,
    Root,
    UnknownTypeError,
)
from .proto_parser import ProtoParseError, parse_proto

# Errors
from .errors import (
    AlreadyConnectedError,
    AlreadyInitializedError,
    AlreadyStartedError,
    ChannelClosedError,
    ConnectionError,
    CustomTypeConflictError,
    DisconnectedError,
    HandledError,
    InternalServiceError,
    InvalidMessageError,
    InvalidMessageIdError,
    InvalidMethodError,
    InvalidMethodNameError,
    InvalidPriorityError,
    InvalidRequestError,
    InvalidResponseError,
    InvalidResultError,
    InvalidServiceNameError,
    MessageTypeRequiredError,
    MissingExchangeError,
    MissingProto,
    MissingProtoError,
    NotConnectedError,
    NotInitializedError,
    NotReadyError,
    ProtocolError,
    PublishConfirmTimeoutError,
    PublishError,
    PublishMessageError,
    PublishNackedError,
    ReconnectionError,
    RetryQueueMismatchError,
    RpcTimeoutError,
    StreamBackpressureError,
    StreamClosedError,
    StreamSequenceError,
    StreamTimeoutError,
    StreamingError,
    TimeoutError,
    UnknownMethodError,
    UnroutableError,
    is_handled_error,
    safe_error_summary,
    sanitize_error_for_client,
)

# Custom types
from .custom_types import (
    BIGINT_BYTES,
    BIGINT_MAX,
    BigIntType,
    CustomType,
    ICustomType,
    TimestampType,
    bigint_to_bytes,
    bytes_to_bigint,
    get_custom_type,
    get_custom_type_names,
    is_custom_type,
    register_custom_type,
)

# Logging
from .logger import (
    DefaultLogger,
    ILogger,
    IStructuredLogger,
    Log,
    LogDiagnostics,
    LogLevel,
    LogRecord,
    Logger,
    format_log_record,
    get_log_level,
    get_logger,
    redact_url,
    set_diagnostics_serializer,
    set_log_level,
    set_logger,
)

# Config and priority
from .config import Config
from .priority import validate_max_priority, validate_message_priority, validate_priority

__version__ = "2.0.0"

__all__ = [
    "__version__",
    # Context
    "Context", "ContextOptions", "IContext",
    # Services
    "MessageService", "MessageServiceOptions", "IMessageServiceOptions", "RetryOptions", "RetryConfig",
    "DEFAULT_RETRY_OPTIONS", "ProxiedService", "ServiceProxy", "StreamingCall", "RemoteError",
    "RunnableService", "ServiceCluster",
    # Events
    "EventHandler", "EventListener", "EventRetryOptions",
    # Connection
    "Connection", "ConsumeOptions", "ConsumeRetryOptions", "IConnection", "MessageHandler",
    "MessageHandlerContext", "MessageHandlerResult", "ReconnectionOptions", "Restorer",
    "apply_heartbeat", "attach_restorer", "AbortController", "AbortSignal",
    # Dispatchers
    "CallOptions", "StreamOptions", "StreamingReply",
    # Factory
    "MessageFactory", "Root", "RequestContainer", "ResponseContainer", "ResponseResult",
    "ResponseError", "EventContainer", "UnknownTypeError", "ProtoParseError", "parse_proto",
    # Errors
    "AlreadyConnectedError", "AlreadyInitializedError", "AlreadyStartedError", "ChannelClosedError",
    "ConnectionError", "CustomTypeConflictError", "DisconnectedError", "HandledError",
    "InternalServiceError", "InvalidMessageError", "InvalidMessageIdError", "InvalidMethodError",
    "InvalidMethodNameError", "InvalidPriorityError", "InvalidRequestError", "InvalidResponseError",
    "InvalidResultError", "InvalidServiceNameError", "MessageTypeRequiredError", "MissingExchangeError",
    "MissingProto", "MissingProtoError", "NotConnectedError", "NotInitializedError", "NotReadyError",
    "ProtocolError", "PublishConfirmTimeoutError", "PublishError", "PublishMessageError",
    "PublishNackedError", "ReconnectionError", "RetryQueueMismatchError", "RpcTimeoutError",
    "StreamBackpressureError", "StreamClosedError", "StreamSequenceError", "StreamTimeoutError",
    "StreamingError", "TimeoutError", "UnknownMethodError", "UnroutableError", "is_handled_error",
    "safe_error_summary", "sanitize_error_for_client",
    # Custom types
    "BIGINT_BYTES", "BIGINT_MAX", "BigIntType", "CustomType", "ICustomType", "TimestampType",
    "bigint_to_bytes", "bytes_to_bigint", "get_custom_type", "get_custom_type_names", "is_custom_type",
    "register_custom_type",
    # Logging
    "DefaultLogger", "ILogger", "IStructuredLogger", "Log", "LogDiagnostics", "LogLevel", "LogRecord",
    "Logger", "format_log_record", "get_log_level", "get_logger", "redact_url",
    "set_diagnostics_serializer", "set_log_level", "set_logger",
    # Config / priority
    "Config", "validate_max_priority", "validate_message_priority", "validate_priority",
]
