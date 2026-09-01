"""Message service - base class for RPC-based microservices."""

import inspect
import os
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Type

from .connection import RetryOptions
from .context import IContext
from .errors import (
    HandledError,
    InvalidMethodError,
    InvalidResultError,
    MissingProtoError,
    ProtocolError,
)
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

    def _declared_methods(self) -> Optional[set]:
        """
        The method names this service's .proto declares, if one is loaded.

        Returns None when no schema is available — the JSON mode this port
        supports, where there is nothing to check a name against.
        """
        service = self._context.factory.lookup_service(self.service_name)
        if service is None:
            return None
        try:
            return {m.name for m in service.methods}
        except Exception:  # pragma: no cover - descriptor shape guard
            return None

    def _own_handler(self, method_name: str) -> Optional[Callable[..., Any]]:
        """
        Resolve a handler against what this service itself implements.

        ``getattr(self, name)`` walked the whole MRO, so every member of
        MessageService, RunnableService, ProxiedService, ABC and object was
        addressable from the bus: naming ``init``, ``publish_event``,
        ``cleanup`` or ``_on_message`` reached them. The walk now stops at
        MessageService — the same boundary TS protobus draws in 4085332.

        Resolution goes through each class's ``__dict__`` rather than
        ``getattr`` so that a property is not evaluated just to decide whether
        it is addressable.
        """
        if not method_name or method_name.startswith("_"):
            return None

        for klass in type(self).__mro__:
            if klass in (MessageService, ABC, object):
                break
            if method_name in klass.__dict__:
                handler = getattr(self, method_name, None)
                return handler if callable(handler) else None
        return None

    def _resolve_contract_method(
        self, body_method: str, routing_key: Optional[str]
    ) -> str:
        """
        Validate the request's method against this service's own contract.

        The handler used to be chosen with ``getattr(self, body_method.split('.')[-1])``
        while nothing checked the rest of the name, the owning service, or the
        routing key RabbitMQ actually authorised. A publisher holding no more
        than the ordinary right to call this service could therefore append a
        segment to redirect dispatch, name another service's method to have the
        payload parsed under a foreign schema, or reach an inherited member.

        Raises:
            InvalidMethodError: If the name is not this contract's method.
        """
        service_part, _, method_name = body_method.rpartition(".")

        if not method_name or service_part != self.service_name:
            raise InvalidMethodError(
                f"Request method {body_method!r} is not a method of "
                f"{self.service_name}"
            )

        # The routing key is what RabbitMQ's topic permissions are granted
        # against. If the body may name a different method than the key it
        # arrived on, those permissions are unenforceable.
        if routing_key:
            expected = f"REQUEST.{self.service_name}.{method_name}"
            # A message coming back from the retry queue is dead-lettered with
            # the queue's static x-dead-letter-routing-key, which names the
            # whole service rather than one method. That key authorises the
            # same set the queue binding does, so it is accepted; see the
            # known-debt note about giving retries their own exchange so the
            # original method segment survives the hop.
            retry_key = f"REQUEST.{self.service_name}.*"
            if routing_key not in (expected, retry_key):
                raise InvalidMethodError(
                    f"Request method {body_method!r} disagrees with the "
                    f"routing key it arrived on"
                )

        declared = self._declared_methods()
        if declared is not None and method_name not in declared:
            raise InvalidMethodError(
                f"{self.service_name} declares no method {method_name!r}"
            )

        return method_name

    async def _on_message(
        self,
        data: bytes,
        correlation_id: str,
        headers: Optional[dict] = None,
        routing_key: Optional[str] = None,
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
            routing_key: The key the delivery arrived on, when the transport
                supplies one. Enforced against the request body's method.

        Returns:
            Response bytes (unary) or AsyncIterator[bytes] (streaming).
        """
        # Decode the envelope first. This used to run outside any try, so a
        # body that did not parse raised a plain Exception: is_handled_error()
        # said False and the delivery went through three redeliveries and a DLQ
        # publish, while the caller waited out its RPC timeout for a reply the
        # retries were never going to produce. The failure is deterministic —
        # the same bytes fail the same way every time — so answer it now.
        try:
            request = self._context.factory.decode_request(data)
        except Exception as error:
            Logger.error(
                f"Undecodable request on {routing_key or self.service_name} "
                f"({correlation_id}): {type(error).__name__}"
            )
            # The reply does not quote the payload: a payload that failed to
            # decode is still a payload.
            return self._context.factory.build_response(
                "", ProtocolError("request could not be decoded")
            )

        Logger.debug(f"Received request {request.method} ({correlation_id})")

        try:
            method = self._resolve_contract_method(request.method, routing_key)
        except InvalidMethodError as error:
            Logger.warn(
                f"Refused request {request.method!r} on "
                f"{routing_key or self.service_name}: {error}"
            )
            return self._context.factory.build_response(request.method, error)

        handler = self._own_handler(method)

        if handler is None:
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
