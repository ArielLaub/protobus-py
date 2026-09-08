"""
Error classes for Protobus.

Every error the framework can raise is defined here (or, for a few module
specific ones, in the module that raises them and re-exported from the
package root). Each carries a ``code`` where the TypeScript port does, so a
caller can branch on identity rather than on message text.
"""

from typing import Any, Optional

from .config import Config


# ---------------------------------------------------------------------------
# Handled errors — answered, never retried
# ---------------------------------------------------------------------------


class HandledError(Exception):
    """
    Base class for expected errors that should NOT trigger a retry.

    When a service method raises a HandledError (or any subclass), the error
    is returned to the caller and the message is settled without going
    through the retry ladder. Use it for validation failures, business-logic
    rejections and anything else that will fail the same way on redelivery.

    Example::

        class ValidationError(HandledError):
            def __init__(self, message):
                super().__init__(message, 'VALIDATION_ERROR')
    """

    is_handled: bool = True

    def __init__(self, message: str, code: str = "HANDLED_ERROR"):
        super().__init__(message)
        self.message = message
        self.code = code

    @property
    def name(self) -> str:
        return type(self).__name__


def is_handled_error(error: Any) -> bool:
    """
    True for a HandledError, or anything duck-typed with ``is_handled = True``.
    """
    if isinstance(error, HandledError):
        return True
    return isinstance(error, BaseException) and getattr(error, "is_handled", False) is True


class ProtocolError(HandledError):
    """
    The message could not be understood: it did not decode, or it named
    something this service does not serve.

    Handled by definition: a malformed message is malformed every time it is
    delivered, so the retry ladder buys identical failures and a DLQ entry
    while the caller waits for a reply the retries were never going to produce.
    """

    def __init__(self, message: str, code: str = "PROTOCOL_ERROR"):
        super().__init__(message, code)


class InternalServiceError(Exception):
    """
    Substituted for an unhandled service error before it crosses back to the
    caller, unless ``Config.expose_internal_errors()`` is enabled.

    Carries the correlation id so an operator can join the caller's report to
    the real exception in the service's own log.
    """

    code = "INTERNAL_ERROR"

    def __init__(self, correlation_id: Optional[str] = None):
        super().__init__(
            f"internal service error (correlationId {correlation_id})"
            if correlation_id
            else "internal service error"
        )
        self.correlation_id = correlation_id


def sanitize_error_for_client(error: Any, correlation_id: Optional[str] = None) -> Any:
    """
    Decide what an error looks like to the *caller*.

    A HandledError passes through untouched. Anything else is an internal
    failure whose message was written for the service's own logs and may
    quote the very data that caused it — that becomes a generic
    InternalServiceError unless internal errors are configured to cross.
    """
    if is_handled_error(error):
        return error
    if Config.expose_internal_errors():
        return error
    return InternalServiceError(correlation_id)


def safe_error_summary(error: Any) -> str:
    """
    A non-disclosing description of an error: its class name and ``code``,
    never its message — except for a HandledError, which a service chose to
    expose.

    Meant for places the text travels further than the process: retry/DLQ
    metadata headers, which ops dashboards read and which persist in a queue.
    """
    if error is None:
        return "UnknownError"
    if is_handled_error(error):
        code = getattr(error, "code", "HANDLED_ERROR")
        return f"{type(error).__name__}[{code}]: {error}"
    name = type(error).__name__ if isinstance(error, BaseException) else "Error"
    code = getattr(error, "code", None)
    return f"{name}[{code}]" if code else name


def error_message(error: Any) -> str:
    """The text of an exception, without ``str()`` of a bare class name."""
    if error is None:
        return "None"
    return getattr(error, "message", None) or str(error) or type(error).__name__


# ---------------------------------------------------------------------------
# Connection errors
# ---------------------------------------------------------------------------


class AlreadyConnectedError(Exception):
    """connect() was called on a connection that is already connected."""


class TimeoutError(Exception):  # noqa: A001 - mirrors the TS name
    """A handler exceeded its processing budget."""

    code = "PROCESSING_TIMEOUT"


class ReconnectionError(Exception):
    """A reconnection attempt failed, or the connection was torn down mid-way."""


class NotReadyError(Exception):
    """
    The connection is not carrying traffic: reconnecting, closed, or given up.
    Distinct from a publish failure — nothing was attempted.
    """

    code = "NOT_READY"


class DisconnectedError(Exception):
    """The connection was lost while an RPC call was waiting for its reply."""

    def __init__(self, message: str = "Connection lost during RPC call"):
        super().__init__(message)


class NotConnectedError(Exception):
    """An operation needs a connection and there is none."""


class ConnectionError(Exception):  # noqa: A001
    """A listener was initialised against a connection that is not up."""


# ---------------------------------------------------------------------------
# Lifecycle errors
# ---------------------------------------------------------------------------


class NotInitializedError(Exception):
    """An operation was attempted before init()."""


class AlreadyInitializedError(Exception):
    """init() was called twice."""


class AlreadyStartedError(Exception):
    """start() was called on a listener that is already consuming."""


class MissingExchangeError(Exception):
    """A listener was initialised with no exchange name."""


# ---------------------------------------------------------------------------
# Message / schema errors
# ---------------------------------------------------------------------------


class MessageTypeRequiredError(Exception):
    """decode_message() was called with no type name."""


class InvalidMethodNameError(Exception):
    """A name that is not of the form ``<package>.<Service>.<method>``."""


class UnknownMethodError(Exception):
    """A well-formed name whose method is not declared by the named service."""


class InvalidMessageError(Exception):
    """An event could not be encoded."""


class InvalidRequestError(Exception):
    """A request could not be encoded."""


class InvalidResponseError(Exception):
    """A response could not be decoded."""


class InvalidServiceNameError(Exception):
    """No service in the schema matches the name, or a method name collides."""


class MissingProto(Exception):
    """
    The .proto backing a service could not be read, or declares no service
    matching its ServiceName.
    """


# 1.x name, kept as an alias.
MissingProtoError = MissingProto


class InvalidMethodError(ProtocolError):
    """
    The request named a method this service does not serve. A ProtocolError
    so the connection layer answers the caller instead of retrying.
    """

    def __init__(self, message: str, code: str = "PROTOCOL_ERROR"):
        super().__init__(message, code)


class InvalidResultError(Exception):
    """A service method returned something that is not a result."""


class InvalidPriorityError(ValueError):
    """
    A ``max_priority`` or per-message ``priority`` that AMQP cannot carry.

    Raised client-side, before anything reaches the broker: an out-of-range
    ``x-max-priority`` is a 406 that closes the channel, and a non-integer
    per-message priority is silently truncated by the encoder.
    """


class InvalidMessageIdError(Exception):
    """A ``message_id`` was supplied that cannot identify anything."""


class CustomTypeConflictError(Exception):
    """A custom type name was re-registered with a different wire type."""


# ---------------------------------------------------------------------------
# RPC / publish outcomes
# ---------------------------------------------------------------------------


class RpcTimeoutError(Exception):
    """
    A unary RPC call got no reply within its timeout.

    Happens whenever nothing is bound to the routing key, the exchange drops
    the message, or the handler dies without replying.
    """

    code = "RPC_TIMEOUT"


class PublishError(Exception):
    """
    Base class for publish failures.

    ``message_id`` is stable across retries of the same logical message, so a
    consumer can deduplicate on it.
    """

    def __init__(self, message: str, message_id: Optional[str] = None):
        super().__init__(message)
        self.message_id = message_id


class PublishNackedError(PublishError):
    """
    The broker explicitly refused the message (basic.nack). A definite
    negative outcome: the message was NOT stored, and republishing is safe.
    """

    code = "PUBLISH_NACKED"


class UnroutableError(PublishError):
    """
    A ``mandatory`` publish reached the exchange but matched no queue. For an
    RPC request this usually means no service is bound to the routing key.
    """

    code = "UNROUTABLE"


class PublishConfirmTimeoutError(PublishError):
    """
    No confirm arrived within the configured window. The outcome is UNKNOWN —
    the broker may or may not have stored the message — so a retry can
    duplicate it. Consumers must be idempotent; see docs on message identity.
    """

    code = "PUBLISH_CONFIRM_TIMEOUT"


class ChannelClosedError(PublishError):
    """
    The channel closed while a publish was awaiting its confirm. Like a
    confirm timeout this is an ambiguous outcome, not a definite failure.
    """

    code = "CHANNEL_CLOSED"


# 1.x name. Publish failures are now raised with their real type; this stays
# importable so a ``except PublishMessageError`` keeps compiling, and is a
# PublishError so such a handler still catches what it used to.
PublishMessageError = PublishError


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class StreamingError(Exception):
    """Base class for streaming RPC errors. See docs/advanced/streaming.md."""


class StreamTimeoutError(StreamingError):
    """No chunk arrived within the idle timeout."""


class StreamBackpressureError(StreamingError):
    """A stream's buffer exceeded its configured chunk or byte bound."""


class StreamSequenceError(StreamingError):
    """
    A streaming reply arrived with a gap in its sequence numbers, meaning at
    least one chunk was lost. Failing is deliberate: yielding what did arrive
    hands the caller a short stream that looks like a complete one.
    """


class StreamClosedError(StreamingError):
    """
    Deprecated: never raised, scheduled for removal in 3.0. Every ending a
    stream can have already has a defined outcome (DisconnectedError,
    StreamTimeoutError, a clean end on cancellation).
    """


# ---------------------------------------------------------------------------
# Retry topology
# ---------------------------------------------------------------------------


class RetryQueueMismatchError(Exception):
    """
    The retry queue exists with arguments that differ from what this service
    is configured for — in practice, a changed ``retry_delay_ms``.
    """
