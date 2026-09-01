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


class InvalidMethodError(Exception):
    """Raised when a service method is invalid."""
    pass


class InvalidResultError(Exception):
    """Raised when a service method returns an invalid result."""
    pass


class InvalidPriorityError(ValueError):
    """
    Raised for an out-of-range or non-integer queue/message priority.

    Subclasses ValueError because that is what a caller would already be
    catching around a bad argument, and because both of the underlying
    failures it replaces are value problems:

    - ``x-max-priority`` outside 1..255 is a 406 PRECONDITION_FAILED at
      declare time, which closes the channel it was made on. Other listeners
      on the same connection survive that, but the declare happens inside
      init(), so the listener never starts and the service fails to boot.
    - A message ``priority`` outside 0..255 raises a raw ``struct.error``
      from deep inside the AMQP encoder, and a *non-integer* one is worse:
      aio-pika does ``int(priority)``, so 1.5 is silently stored as 1.

    Validating at our own seam turns all of that into one clear error, and
    keeps the Python port's behaviour identical to the TypeScript port's.
    """
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


# Streaming errors
class StreamingError(Exception):
    """Base class for streaming RPC errors."""
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
