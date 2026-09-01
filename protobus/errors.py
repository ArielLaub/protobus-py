"""Error classes for Protobus."""

from typing import Any, Optional


class HandledError(Exception):
    """
    Base class for expected errors that shouldn't trigger retries.

    Use this for validation errors, business logic failures, and other
    expected error conditions that should not be retried.

    Example:
        class ValidationError(HandledError):
            pass

        def validate_user(data):
            if not data.get('email'):
                raise ValidationError('Email is required', code='MISSING_EMAIL')
    """

    is_handled: bool = True

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.code = code or "HANDLED_ERROR"
        self.message = message


def is_handled_error(error: Any) -> bool:
    """
    Check if an error is a handled error.

    Supports both isinstance checking and duck typing for compatibility.
    """
    if isinstance(error, HandledError):
        return True
    return getattr(error, "is_handled", False) is True


# Connection errors
class AlreadyConnectedError(Exception):
    """Raised when attempting to connect when already connected."""
    pass


class TimeoutError(Exception):
    """Raised when an operation times out."""
    pass


class ReconnectionError(Exception):
    """Raised when reconnection fails."""
    pass


class DisconnectedError(Exception):
    """Raised when operation fails due to disconnection."""
    pass


class NotConnectedError(Exception):
    """Raised when attempting operations without a connection."""
    pass


class NotInitializedError(Exception):
    """Raised when attempting operations before initialization."""
    pass


class AlreadyInitializedError(Exception):
    """Raised when attempting to initialize twice."""
    pass


class AlreadyStartedError(Exception):
    """Raised when attempting to start something already started."""
    pass


# Message errors
class MessageTypeRequiredError(Exception):
    """Raised when message type is required but not provided."""
    pass


class InvalidMessageError(Exception):
    """Raised when a message is invalid."""
    pass


class InvalidRequestError(Exception):
    """Raised when a request is invalid."""
    pass


class InvalidResponseError(Exception):
    """Raised when a response is invalid."""
    pass


# Service errors
class InvalidServiceNameError(Exception):
    """Raised when a service name is invalid."""
    pass


class InvalidMethodError(HandledError):
    """
    Raised when a request names a method the receiving service does not declare.

    A HandledError: naming a method that does not exist is deterministic, so
    retrying the delivery cannot change the outcome. The caller is answered and
    the delivery is dropped rather than put through the retry ladder and the
    DLQ. Parity with TS protobus 6c9b12d.
    """

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message, code or "INVALID_METHOD")


class InvalidResultError(Exception):
    """Raised when a service method returns an invalid result."""
    pass


class PublishMessageError(Exception):
    """Raised when publishing a message fails."""
    pass


class MissingProtoError(Exception):
    """Raised when proto file is missing."""
    pass


class MissingExchangeError(Exception):
    """Raised when exchange is missing."""
    pass


class ConnectionError(Exception):
    """General connection error."""
    pass


class ProtocolError(HandledError):
    """
    Raised when an envelope or payload cannot be decoded.

    A HandledError: the same bytes fail the same way every time, so the retry
    ladder buys five broker operations and a DLQ entry for nothing while the
    caller waits out its RPC timeout for a reply the retries were never going
    to produce. Parity with TS protobus 6c9b12d.

    The message never quotes the payload — a payload that failed to decode is
    still a payload.
    """

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message, code or "PROTOCOL_ERROR")


class RpcTimeoutError(TimeoutError):
    """
    Raised when a unary RPC is not answered within the caller's deadline.

    Distinct from the server-side message processing timeout: this is the
    caller's budget for the whole round trip. Parity with TS protobus 908d5c8.
    """
    pass


class PublishError(Exception):
    """Base class for failures to get a message onto the bus."""
    pass


class UnroutableError(PublishError):
    """
    Raised when the broker returned a mandatory publish as unroutable.

    A definite failure — the message reached the broker and was not enqueued
    anywhere — so it is safe to retry. This is the failure that makes a service
    with no consumers look like a timeout rather than an error.
    """
    pass


# Streaming errors
class StreamingError(Exception):
    """Base class for streaming RPC errors."""
    pass


class StreamSequenceError(StreamingError):
    """
    Raised when a streaming reply arrives with a gap in its sequence numbers.

    A missing chunk would otherwise be delivered as a shorter but apparently
    complete stream. Parity with TS protobus 1e829ad ("sequence gaps raise
    instead of yielding a short stream").
    """
    pass


class StreamTimeoutError(StreamingError):
    """Raised when no streaming chunk arrives within the idle timeout."""
    pass


class StreamBackpressureError(StreamingError):
    """Raised when the streaming reply queue overflows."""
    pass


class StreamClosedError(StreamingError):
    """Raised when iterating a stream after it has been closed."""
    pass
