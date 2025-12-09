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
