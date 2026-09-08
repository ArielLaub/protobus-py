"""MessageService: the base class every RPC service extends."""

import inspect
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, Optional, Set

from .cancel_listener import CancelListener
from .connection import RESPONSE_BUFFER_ATTR, MessageHandlerContext
from .context import IContext
from .errors import (
    HandledError,
    InvalidMethodError,
    InvalidResultError,
    MissingProto,
    ProtocolError,
    TimeoutError,
    is_handled_error,
    sanitize_error_for_client,
)
from .event_listener import EventHandler, EventListener, EventRetryOptions
from .logger import Logger
from .message_factory import MessageFactory
from .message_listener import DEFAULT_RETRY_OPTIONS, MessageListener, RetryOptions

__all__ = [
    "MessageService",
    "MessageServiceOptions",
    "IMessageServiceOptions",
    "RetryOptions",
    "DEFAULT_RETRY_OPTIONS",
    "HandledError",
    "is_handled_error",
    "MissingProto",
    "InvalidMethodError",
    "InvalidResultError",
]


def _last_segment(value: str) -> str:
    i = value.rfind(".")
    return value if i == -1 else value[i + 1:]


@dataclass
class MessageServiceOptions:
    """
    Options for a MessageService.

    ``late_ack``: ack after the handler completes (default) rather than on
    delivery. Acking on delivery disables retry, DLQ and the error reply
    entirely — the message is dropped and the caller waits for a reply that
    never comes. Set False only for genuine at-most-once delivery.

    ``max_priority``: declare the request queue as a RabbitMQ priority queue.
    Opt-in; left unset the queue arguments are byte-identical to before.

    ``event_retry``: retry for this service's EVENT subscriptions, separate
    from ``retry`` and off by default. See EventRetryOptions.
    """

    max_concurrent: Optional[int] = None
    retry: Optional[RetryOptions] = None
    late_ack: bool = True
    processing_timeout_ms: Optional[int] = None
    max_priority: Optional[int] = None
    event_retry: Optional[EventRetryOptions] = None


# TS parity name.
IMessageServiceOptions = MessageServiceOptions


class MessageService:
    """
    Serves the methods its .proto declares on ``REQUEST.<ServiceName>.*``.

    Subclasses define ``service_name`` (or ``ServiceName``) and
    ``proto_file_name`` (or ``ProtoFileName``), and one ``async def`` per
    rpc, called as ``method(request, actor, correlation_id, context)`` — the
    fourth argument is passed only to a method that declares it.
    """

    def __init__(self, context: IContext, options: Optional[MessageServiceOptions] = None, **kwargs: Any) -> None:
        if options is None:
            options = MessageServiceOptions(**kwargs)
        elif kwargs:
            raise TypeError("pass either options or keyword options, not both")
        self.context = context
        self._options = options
        self._retry_options = RetryOptions(
            max_retries=options.retry.max_retries if options.retry else DEFAULT_RETRY_OPTIONS.max_retries,
            retry_delay_ms=options.retry.retry_delay_ms if options.retry else DEFAULT_RETRY_OPTIONS.retry_delay_ms,
            message_ttl_ms=options.retry.message_ttl_ms if options.retry else None,
        )
        self._listener = MessageListener(
            context.connection,
            options.late_ack if options.late_ack is not None else True,
            options.max_concurrent,
            self._retry_options,
            options.processing_timeout_ms,
            options.max_priority,
        )
        self._event_listener = EventListener(context.connection, context.factory, options.event_retry)
        self._cancel_listener = CancelListener(context.connection)
        # The service as its .proto declares it, which is not always
        # ServiceName: instances sharing one schema are addressed under
        # distinct runtime names (`Combat.Player.player6` serving the
        # contract `Combat.Player`).
        self._contract_service_name: Optional[str] = None
        self._declared_methods: Optional[Set[str]] = None

    # -- naming ---------------------------------------------------------------

    @property
    def service_name(self) -> str:
        """Runtime name of this service. Subclasses override this or ``ServiceName``."""
        cls = type(self)
        if cls.ServiceName is not MessageService.ServiceName:
            return self.ServiceName
        raise NotImplementedError(f"{cls.__name__} must define service_name")

    @property
    def ServiceName(self) -> str:  # noqa: N802 - TS parity
        cls = type(self)
        if cls.service_name is not MessageService.service_name:
            return self.service_name
        raise NotImplementedError(f"{cls.__name__} must define service_name")

    @property
    def proto_file_name(self) -> str:
        cls = type(self)
        if cls.ProtoFileName is not MessageService.ProtoFileName:
            return self.ProtoFileName
        raise NotImplementedError(f"{cls.__name__} must define proto_file_name")

    @property
    def ProtoFileName(self) -> str:  # noqa: N802
        cls = type(self)
        if cls.proto_file_name is not MessageService.proto_file_name:
            return self.proto_file_name
        raise NotImplementedError(f"{cls.__name__} must define proto_file_name")

    @property
    def Proto(self) -> str:  # noqa: N802
        """The .proto source, read from ``proto_file_name``."""
        path = self.proto_file_name
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        raise MissingProto("missing_proto_source")

    @property
    def proto(self) -> str:
        return self.Proto

    @property
    def contract_service_name(self) -> Optional[str]:
        return self._contract_service_name

    @property
    def listener(self) -> MessageListener:
        return self._listener

    @property
    def event_listener(self) -> EventListener:
        return self._event_listener

    @property
    def cancel_listener(self) -> CancelListener:
        return self._cancel_listener

    # -- events ---------------------------------------------------------------

    async def publish_event(self, event_type: str, content: Any, topic: Optional[str] = None) -> None:
        await self.context.publish_event(event_type, content, topic)

    async def subscribe_event(self, event_type: str, handler: EventHandler, topic: Optional[str] = None) -> Any:
        return await self._event_listener.subscribe(event_type, handler, topic)

    # -- schema ---------------------------------------------------------------

    def _register_schema(self) -> None:
        """Make sure this service's schema is in the factory's root. Skipped
        when already present, so passing a proto directory as well works."""
        factory = self.context.factory
        if factory.has_service(self.service_name):
            return
        factory.parse(self.Proto, self.service_name)

    def _resolve_contract(self) -> None:
        """
        Find the contract this service serves, by trimming runtime segments
        off service_name until one names a service in the root.
        """
        if self._contract_service_name is not None:
            return
        factory = self.context.factory
        candidate = self.service_name
        while True:
            if factory.has_service(candidate):
                self._contract_service_name = candidate
                self._declared_methods = set(factory.get_service_method_names(candidate))
                return
            cut = candidate.rfind(".")
            if cut <= 0:
                raise MissingProto(
                    f"no service in the schema matches '{self.service_name}' or any prefix of it; "
                    "the .proto must declare the service this class serves"
                )
            candidate = candidate[:cut]

    def _resolve_own_handler(self, name: str) -> Optional[Callable[..., Any]]:
        """
        The implementation of ``name`` defined by THIS service, or None.

        The walk stops at the framework's own classes, so a declared rpc can
        only ever reach something the subclass wrote. A plain ``getattr``
        would resolve an rpc named ``init`` or ``publish_event`` to the
        framework's member and call it with the caller's arguments.
        """
        if not name or name.startswith("_"):
            return None
        for klass in type(self).__mro__:
            module = getattr(klass, "__module__", "") or ""
            if module == "protobus" or module.startswith("protobus."):
                break
            if klass is object:
                break
            raw = klass.__dict__.get(name)
            if raw is None:
                continue
            if not inspect.isroutine(raw) and not isinstance(raw, (staticmethod, classmethod)):
                return None
            handler = getattr(self, name, None)
            return handler if callable(handler) else None
        return None

    # -- lifecycle ------------------------------------------------------------

    async def init(self) -> None:
        try:
            self._register_schema()
            self._resolve_contract()
            await self._listener.init(self._on_message, self.service_name)
            await self._event_listener.init(None, f"{self.service_name}.Events")
            await self._listener.subscribe(f"REQUEST.{self.service_name}.*")
            await self._listener.start()
            await self._event_listener.start()
            # Started last: it only matters once requests can arrive.
            await self._cancel_listener.start()
        except Exception as err:
            Logger.error(f"error initializing service {self.service_name} - {err}")
            raise

    async def stop_consuming(self) -> None:
        """
        Stop accepting new requests and events, leaving channels open so
        work already in hand can finish. The first step of a graceful
        shutdown; pair it with ``connection.drain_in_flight()`` before
        closing anything.
        """
        await self._listener.stop_consuming()
        await self._event_listener.stop_consuming()
        # Closed with the rest: a drained service has no stream left to cancel.
        await self._cancel_listener.close()

    async def close(self) -> None:
        """Stop consuming and release every channel this service holds."""
        await self.stop_consuming()
        for listener in (self._listener, self._event_listener):
            if listener.is_initialized:
                try:
                    await listener.close()
                except Exception as err:
                    Logger.debug(f"{self.service_name}: error closing listener: {err}")

    # -- dispatch -------------------------------------------------------------

    async def _on_message(
        self,
        data: bytes,
        correlation_id: str,
        _headers: Optional[Dict[str, Any]] = None,
        context: Optional[MessageHandlerContext] = None,
    ) -> Any:
        """Core handler for requests made to ``REQUEST.<ServiceName>.*``."""
        self._resolve_contract()
        factory: MessageFactory = self.context.factory
        routing_key = context.routing_key if context is not None else None

        # Envelope first, payload later. The envelope names the method, and
        # that name selects the schema the payload is read with — so it has
        # to be checked against this service's contract before the bytes are
        # interpreted.
        try:
            envelope = factory.decode_request_envelope(data)
        except Exception:
            Logger.error(
                f"unparseable request envelope on {self.service_name} ({len(data) if data else 0} bytes, {correlation_id})"
            )
            return self._protocol_error(routing_key, "request envelope did not decode")
        Logger.debug(f"received request {envelope.method} ({correlation_id})")

        # A rejection is reported against the method the ROUTING KEY names,
        # not the one the body asked for: the body's name is what is in dispute.
        contract_method = f"{self._contract_service_name}.{_last_segment(routing_key) if routing_key else _last_segment(envelope.method)}"

        def reject_dispatch(reason: str) -> bytes:
            Logger.error(reason)
            return factory.build_response(contract_method, InvalidMethodError(reason))

        # 1. The delivery belongs to THIS service — checked against the routing
        #    key the broker used, since the body is publisher-controlled.
        # 2. The method the body asks for is the method the routing key names,
        #    so RabbitMQ topic permissions stay meaningful.
        if routing_key:
            if not routing_key.startswith(f"REQUEST.{self.service_name}."):
                return reject_dispatch(f"routing key {routing_key} does not belong to service {self.service_name}")
            if _last_segment(routing_key) != _last_segment(envelope.method):
                return reject_dispatch(f"request method {envelope.method} contradicts routing key {routing_key}")

        # 3. The body names a method of THIS contract, spelled in full.
        try:
            parsed_service, method = MessageFactory.split_method_name(envelope.method)
        except Exception:
            return reject_dispatch(f"request method {envelope.method} is not a qualified method name")
        if parsed_service != self._contract_service_name:
            return reject_dispatch(f"request method {envelope.method} is not a method of {self._contract_service_name}")
        assert self._declared_methods is not None
        if method not in self._declared_methods:
            return reject_dispatch(f"{self._contract_service_name} declares no method {method}")

        handler = self._resolve_own_handler(method)
        if handler is None:
            error = InvalidMethodError(f"invalid service method {method}")
            Logger.error(error.message)
            return factory.build_response(envelope.method, error)

        # Validated: the payload can now be read against the contract's schema.
        try:
            request_data = factory.decode_request_payload(envelope.method, envelope.data)
        except Exception:
            Logger.error(
                f"unparseable request payload for {envelope.method} ({len(envelope.data) if envelope.data else 0} bytes, {correlation_id})"
            )
            return self._protocol_error(envelope.method, f"payload did not decode as the request type of {envelope.method}")

        # A processing timeout is raised by the connection layer, which has
        # no factory to encode a reply with. Leave one ready, so the caller is
        # told rather than left to wait out its own deadline.
        if context is not None:
            try:
                context.timeout_reply = factory.build_response(
                    envelope.method, TimeoutError(f"message {correlation_id} exceeded the processing timeout"),
                )
            except Exception as encode_err:
                Logger.debug(f"could not pre-encode a timeout reply for {envelope.method}: {encode_err}")

        args = (request_data, envelope.actor, correlation_id)
        if _handler_wants_context(handler):
            args = args + (context,)  # type: ignore[assignment]

        # Streaming path: the .proto declares this method server-streaming, so
        # the handler must produce an async iterable of chunks.
        if factory.is_streaming_method(envelope.method):
            try:
                iterable = handler(*args)
                if inspect.isawaitable(iterable) and not hasattr(iterable, "__aiter__"):
                    iterable = await iterable
            except Exception as error:
                return self._handle_unary_error(envelope.method, error, correlation_id)
            if not hasattr(iterable, "__aiter__"):
                error = InvalidResultError(f"streaming method {method} must return an async iterable")
                return factory.build_response(envelope.method, error)
            return self._stream_responses(envelope.method, iterable)

        # Unary path.
        try:
            result = handler(*args)
            if inspect.isawaitable(result):
                result = await result
        except Exception as error:
            return self._handle_unary_error(envelope.method, error, correlation_id)
        Logger.debug(f"sending result {envelope.method} ({correlation_id})")
        return factory.build_response(envelope.method, result)

    def _protocol_error(self, label: Optional[str], reason: str) -> bytes:
        """Answer a message this service could not understand, without
        retrying it: the same bytes fail identically every time."""
        return self.context.factory.build_response(label or "unknown", ProtocolError(reason))

    def _handle_unary_error(self, method: str, error: BaseException, correlation_id: str) -> bytes:
        """
        A HandledError is *expected* — validation, business rules — and is
        returned as an error response immediately. Anything else is treated
        as an infrastructure failure and re-raised so the connection layer's
        retry/DLQ machinery takes over; the caller hears back once the
        retries succeed or are exhausted.
        """
        if is_handled_error(error):
            Logger.warn(f"handled error in {method}: {error}")
            return self.context.factory.build_response(method, error)
        # Pre-encode the error reply so the connection layer can answer the
        # caller on the terminal paths without a MessageFactory. What the
        # caller sees is sanitized; what is logged is the real error.
        try:
            setattr(error, RESPONSE_BUFFER_ATTR, self.context.factory.build_response(
                method, sanitize_error_for_client(error, correlation_id),
            ))
        except Exception as encode_err:
            Logger.warn(f"failed to pre-encode error response: {encode_err}")
        Logger.error(f"unhandled error in {method}: {error!r}")
        raise error

    async def _stream_responses(self, method: str, iterable: Any) -> AsyncIterator[bytes]:
        """
        Encode each chunk as a ResponseContainer. An exception during
        iteration becomes a terminal error response, published with
        x-protobus-final=true so the client's iterator raises.
        """
        factory = self.context.factory
        try:
            async for chunk in iterable:
                yield factory.build_response(method, chunk)
        except Exception as error:
            Logger.error(f"error in streaming method {method}: {error!r}")
            yield factory.build_response(method, sanitize_error_for_client(error))


def _handler_wants_context(handler: Callable[..., Any]) -> bool:
    try:
        params = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return True
    positional = [p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 4
