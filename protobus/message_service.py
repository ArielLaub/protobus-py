"""Message service - base class for RPC-based microservices."""

import inspect
import os
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Type

from .connection import RetryOptions
from .context import IContext
from .errors import HandledError, InvalidMethodError, InvalidResultError, MissingProtoError
from .event_listener import EventHandler, EventListener
from .logger import Logger
from .message_listener import MessageListener


# Default retry options
DEFAULT_RETRY_OPTIONS = RetryOptions(
    max_retries=3,
    retry_delay_ms=5000,
    message_ttl_ms=None,
)


class MessageServiceOptions:
    """Options for message service configuration."""

    def __init__(
        self,
        max_concurrent: Optional[int] = None,
        retry: Optional[RetryOptions] = None,
    ):
        self.max_concurrent = max_concurrent
        self.retry = retry or DEFAULT_RETRY_OPTIONS


class MessageService(ABC):
    """
    Abstract base class for RPC-based microservices.

    Subclasses should implement the service methods and define
    ServiceName and ProtoFileName properties.
    """

    def __init__(
        self,
        context: IContext,
        options: Optional[MessageServiceOptions] = None,
    ):
        """
        Initialize the message service.

        Args:
            context: The context to use for messaging
            options: Optional service configuration
        """
        self._context = context
        opts = options or MessageServiceOptions()

        self._retry_options = RetryOptions(
            max_retries=opts.retry.max_retries if opts.retry else 3,
            retry_delay_ms=opts.retry.retry_delay_ms if opts.retry else 5000,
            message_ttl_ms=opts.retry.message_ttl_ms if opts.retry else None,
        )

        self._listener = MessageListener(
            context.connection,
            late_ack=bool(opts.max_concurrent),
            max_concurrent=opts.max_concurrent,
            retry_options=self._retry_options,
        )

        self._event_listener = EventListener(context.connection, context.factory)

    @property
    @abstractmethod
    def service_name(self) -> str:
        """Get the service name."""
        ...

    @property
    @abstractmethod
    def proto_file_name(self) -> str:
        """Get the proto file name/path."""
        ...

    # Aliases for TypeScript API compatibility
    @property
    def ServiceName(self) -> str:
        """Get the service name (TypeScript API compatibility)."""
        return self.service_name

    @property
    def ProtoFileName(self) -> str:
        """Get the proto file name (TypeScript API compatibility)."""
        return self.proto_file_name

    @property
    def Proto(self) -> str:
        """Get the proto file content."""
        proto_file = self.proto_file_name
        if os.path.exists(proto_file):
            with open(proto_file, "r") as f:
                return f.read()
        raise MissingProtoError("missing_proto_source")

    async def publish_event(
        self,
        event_type: str,
        data: Any,
        topic: Optional[str] = None,
    ) -> None:
        """
        Publish an event.

        Args:
            event_type: Type of the event
            data: Event data
            topic: Optional custom topic
        """
        await self._context.publish_event(event_type, data, topic)

    async def subscribe_event(
        self,
        event_type: str,
        handler: EventHandler,
        topic: Optional[str] = None,
    ) -> None:
        """
        Subscribe to events.

        Args:
            event_type: Type of events to subscribe to
            handler: Handler function for the events
            topic: Optional custom topic pattern
        """
        await self._event_listener.subscribe(event_type, handler, topic)

    async def init(self) -> None:
        """Initialize the service."""
        try:
            # Initialize the message listener with our handler
            await self._listener.init(self._on_message, self.service_name)

            # Initialize the event listener
            await self._event_listener.init(None, f"{self.service_name}.Events")

            # Subscribe to requests for this service
            await self._listener.subscribe(f"REQUEST.{self.service_name}.*")

            # Start the listeners
            await self._listener.start()
            await self._event_listener.start()

            Logger.info(f"Service {self.service_name} initialized")

        except Exception as err:
            Logger.error(
                f"Error initializing service {self.service_name} - {err}\n"
                f"{getattr(err, '__traceback__', '')}"
            )
            raise

    async def _on_message(
        self,
        data: bytes,
        correlation_id: str,
        headers: Optional[dict] = None,
    ) -> Any:
        """
        Handle incoming RPC requests.

        Unary handlers (regular ``async def``) return ``bytes``. Streaming
        handlers (``async def`` with ``yield``) return an async iterator of
        ``bytes`` — the framework publishes each chunk as a separate reply
        message with x-protobus-final headers. See docs/advanced/streaming.md.

        Args:
            data: Request data
            correlation_id: Request correlation ID
            headers: Incoming AMQP headers (reserved for future tracing/auth use)

        Returns:
            Response bytes (unary) or AsyncIterator[bytes] (streaming).
        """
        request = self._context.factory.decode_request(data)
        method_parts = request.method.split(".")
        # Method name is always the last part (e.g., "combat.Player.player1.shoot" -> "shoot")
        method = method_parts[-1]

        Logger.debug(f"Received request {request.method} ({correlation_id})")

        # Look up the method on this service. We surface a not-found method
        # as a regular error response — same for unary and streaming.
        handler = getattr(self, method, None)

        if handler is None or not callable(handler):
            err = InvalidMethodError(f"Invalid service method {method}")
            return self._context.factory.build_response(request.method, err)

        # Streaming path: handler is an async-generator function. Each yield
        # gets published as a separate reply chunk by connection._publish_stream_reply.
        if inspect.isasyncgenfunction(handler):
            return self._stream_responses(handler, request, correlation_id)

        # Unary path (existing behavior)
        try:
            result = handler(request.data, request.actor, correlation_id)

            if hasattr(result, "__await__"):
                result = await result
            elif hasattr(result, "then"):
                # For compatibility with JS-style promises
                raise InvalidResultError(
                    "Method returned a non-awaitable promise-like object"
                )

            Logger.debug(f"Sending result {request.method}")
            return self._context.factory.build_response(request.method, result)

        except Exception as error:
            if error:
                Logger.error(
                    getattr(error, "stack", None)
                    or getattr(error, "message", str(error))
                )
            else:
                Logger.error("null error received")

            return self._context.factory.build_response(request.method, error)

    async def _stream_responses(
        self,
        handler: Callable[..., AsyncIterator[Any]],
        request: Any,
        correlation_id: str,
    ) -> AsyncIterator[bytes]:
        """
        Wrap a user async-generator handler so each yielded chunk is encoded
        into a ResponseContainer before being published.

        An exception inside the generator becomes a terminal error response —
        the connection layer publishes it with x-protobus-final=true so the
        client's iterator raises.
        """
        method = request.method
        try:
            async for chunk in handler(request.data, request.actor, correlation_id):
                yield self._context.factory.build_response(method, chunk)
        except Exception as error:
            Logger.error(
                getattr(error, "stack", None)
                or getattr(error, "message", str(error))
            )
            yield self._context.factory.build_response(method, error)
