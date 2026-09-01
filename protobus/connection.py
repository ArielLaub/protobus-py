"""Connection module for RabbitMQ/AMQP connections with automatic reconnection."""

import asyncio
import inspect
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Union

import aio_pika
from aio_pika import ExchangeType, Message
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
)

from .config import Config
from .errors import (
    AlreadyConnectedError,
    DisconnectedError,
    NotConnectedError,
    ReconnectionError,
    UnroutableError,
    is_handled_error,
)
from .logger import Logger

# Type aliases
#
# A handler receives the message body, correlation_id, and the incoming AMQP
# headers — and, if it declares a fourth positional parameter, the routing key
# the delivery arrived on. It returns one of:
#   - bytes  → a single (unary) reply published to message.reply_to
#   - None   → no reply (one-way event, or handler chose to suppress)
#   - AsyncIterator[bytes] → a streaming reply. Each yielded chunk is
#     published to message.reply_to with x-protobus-final=false; the last
#     chunk is published with x-protobus-final=true. See docs/advanced/streaming.md.
MessageHandler = Callable[
    [bytes, str, Dict[str, Any]],
    Awaitable[Any],
]


@dataclass
class ConnectionOptions:
    """Options for connection behavior."""

    max_reconnect_attempts: int = 10
    initial_reconnect_delay_ms: int = 1000
    max_reconnect_delay_ms: int = 30000
    reconnect_backoff_multiplier: float = 2.0
    jitter_percent: float = 0.3


@dataclass
class RetryOptions:
    """Options for message retry behavior."""

    max_retries: int = 3
    retry_delay_ms: int = 5000
    message_ttl_ms: Optional[int] = None


DEFAULT_RETRY_OPTIONS = RetryOptions()


def _handler_accepts_routing_key(handler: Callable[..., Any]) -> bool:
    """
    Whether a message handler declares the optional 4th (routing key) argument.

    Resolved once per consumer rather than per delivery. A handler taking
    *args is assumed to accept it.
    """
    try:
        params = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
        return False

    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return True

    positional = [
        p
        for p in params
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 4


def _is_unroutable(result: Any) -> bool:
    """
    Whether a publish result is the broker returning the message.

    aio-pika sends `mandatory` but reports the outcome in the RETURN VALUE, not
    by raising: a routed publish resolves to a ``Basic.Ack`` frame, an
    unroutable one to a ``DeliveredMessage`` wrapping ``Basic.Return``. Nothing
    in this library ever looked, so every publish to a routing key with no
    binding behind it was dropped in silence — which is what makes a service
    with no consumers look like a timeout rather than an error.

    Detected structurally rather than by importing aiormq/pamqp frame classes,
    so a client upgrade cannot quietly turn this back into a silent drop.
    """
    if result is None:
        return False
    delivery = getattr(result, "delivery", None)
    candidate = delivery if delivery is not None else result
    return type(candidate).__name__ == "Return"


async def publish_confirmed(
    exchange: AbstractExchange,
    message: Message,
    routing_key: str,
    mandatory: bool = True,
) -> None:
    """
    Publish and raise if the broker could not route the message.

    Requests are mandatory: a request nothing is bound to fails at once instead
    of waiting out the caller's RPC timeout. Events are not — having no
    subscribers is normal for an event. Parity with TS protobus 1e829ad.
    """
    result = await exchange.publish(message, routing_key=routing_key, mandatory=mandatory)
    if mandatory and _is_unroutable(result):
        raise UnroutableError(
            f"No queue is bound for routing key {routing_key!r} on exchange "
            f"{getattr(exchange, 'name', '?')!r}; the broker returned the message"
        )


def _redact_url(url: str) -> str:
    """Hide the password in an AMQP URL before it reaches a log."""
    try:
        scheme, _, rest = url.partition("://")
        if not rest or "@" not in rest:
            return url
        creds, _, host = rest.rpartition("@")
        user, sep, _password = creds.partition(":")
        return f"{scheme}://{user}{':***' if sep else ''}@{host}"
    except Exception:  # pragma: no cover - never let logging break a connect
        return "<amqp url>"


def _with_heartbeat(url: str) -> str:
    """
    Ensure the connection negotiates a heartbeat rather than accepting the
    broker's proposal.

    Nothing set one, so the interval was whatever RabbitMQ proposed — 60
    seconds — and a peer that vanishes without closing its socket is only
    noticed after two missed intervals, around two minutes. For all of that
    time the connection reports itself healthy and publishes go into a dead
    socket with no reconnection scheduled. Parity with TS protobus 2fee268.

    A heartbeat already in the URL is the caller being explicit and is left
    alone, ``heartbeat=0`` included — that is how they are turned off. The rest
    of the URL is preserved byte for byte: the vhost is routinely
    percent-encoded and re-encoding it would connect to the wrong one.
    """
    seconds = Config.amqp_heartbeat_seconds()
    if seconds <= 0:
        return url

    base, sep, query = url.partition("?")
    if sep and any(
        param.split("=", 1)[0].strip() == "heartbeat" for param in query.split("&")
    ):
        return url

    joiner = "&" if sep and query else "?"
    return f"{base}{sep if sep and query else ''}{query}{joiner}heartbeat={seconds}"


class IConnection(Protocol):
    """Interface for connection implementations."""

    @property
    def is_connected(self) -> bool:
        ...

    @property
    def is_reconnecting(self) -> bool:
        ...

    async def connect(self, url: str) -> None:
        ...

    async def close(self) -> None:
        ...

    async def open_channel(self) -> AbstractChannel:
        ...

    async def ensure_exchange(
        self,
        channel: AbstractChannel,
        name: str,
        exchange_type: ExchangeType,
    ) -> AbstractExchange:
        ...

    async def ensure_queue(
        self,
        channel: AbstractChannel,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> AbstractQueue:
        ...

    async def bind_queue(
        self,
        queue: AbstractQueue,
        exchange: AbstractExchange,
        routing_key: str,
    ) -> None:
        ...

    async def consume(
        self,
        channel: AbstractChannel,
        queue: AbstractQueue,
        handler: MessageHandler,
        late_ack: bool = False,
        max_concurrent: Optional[int] = None,
        retry_options: Optional[RetryOptions] = None,
    ) -> str:
        ...

    async def publish(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        body: bytes,
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        ...

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        ...


class Connection:
    """
    RabbitMQ connection manager with automatic reconnection and message handling.

    Emits events:
    - 'reconnecting': (attempt: int, max_attempts: int) - reconnection in progress
    - 'reconnected': () - successfully reconnected
    - 'disconnected': () - connection lost
    - 'error': (error: Exception) - connection error
    """

    def __init__(self, options: Optional[ConnectionOptions] = None):
        self._options = options or ConnectionOptions()
        self._connection: Optional[AbstractConnection] = None
        self._url: Optional[str] = None
        self._is_connected = False
        self._is_reconnecting = False
        self._is_closing = False
        self._event_handlers: Dict[str, List[Callable[..., Any]]] = {}
        self._consumer_tags: Dict[str, asyncio.Task] = {}
        self._reconnect_task: Optional[asyncio.Task] = None

    @property
    def is_connected(self) -> bool:
        """Check if connected to RabbitMQ."""
        return self._is_connected

    @property
    def is_reconnecting(self) -> bool:
        """Check if currently reconnecting."""
        return self._is_reconnecting

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        """Register an event handler."""
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(callback)

    def _emit(self, event: str, *args: Any) -> None:
        """Emit an event to all registered handlers."""
        handlers = self._event_handlers.get(event, [])
        for handler in handlers:
            try:
                result = handler(*args)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:
                Logger.error(f"Error in event handler for {event}: {e}")

    async def connect(self, url: str) -> None:
        """
        Connect to RabbitMQ.

        Args:
            url: AMQP connection URL

        Raises:
            AlreadyConnectedError: If already connected
        """
        if self._is_connected:
            raise AlreadyConnectedError("Already connected to RabbitMQ")

        self._url = _with_heartbeat(url)
        Logger.info(f"Connecting to bus: {_redact_url(self._url)}")

        try:
            self._connection = await aio_pika.connect_robust(
                self._url,
                reconnect_interval=self._options.initial_reconnect_delay_ms / 1000,
            )
            self._is_connected = True

            # Set up connection close callback
            self._connection.close_callbacks.add(self._on_connection_closed)

            Logger.info("Connected to RabbitMQ")
        except Exception as e:
            Logger.error(f"Failed to connect: {e}")
            raise

    def _on_connection_closed(
        self, connection: AbstractConnection, exception: Optional[Exception]
    ) -> None:
        """Handle connection closed event."""
        if self._is_closing:
            return

        self._is_connected = False
        Logger.warn("Connection to RabbitMQ lost")
        self._emit("disconnected")

        if not self._is_reconnecting and not self._is_closing:
            self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        if not self._url or self._is_closing:
            return

        self._is_reconnecting = True
        delay = self._options.initial_reconnect_delay_ms

        for attempt in range(1, self._options.max_reconnect_attempts + 1):
            self._emit("reconnecting", attempt, self._options.max_reconnect_attempts)
            Logger.info(
                f"Reconnection attempt {attempt}/{self._options.max_reconnect_attempts}"
            )

            try:
                self._connection = await aio_pika.connect_robust(self._url)
                self._is_connected = True
                self._is_reconnecting = False
                self._connection.close_callbacks.add(self._on_connection_closed)
                Logger.info("Reconnected to RabbitMQ")
                self._emit("reconnected")
                return
            except Exception as e:
                Logger.warn(f"Reconnection attempt {attempt} failed: {e}")

                if attempt < self._options.max_reconnect_attempts:
                    # Add jitter to prevent thundering herd
                    jitter = random.uniform(
                        -self._options.jitter_percent, self._options.jitter_percent
                    )
                    actual_delay = delay * (1 + jitter)
                    await asyncio.sleep(actual_delay / 1000)
                    delay = min(
                        delay * self._options.reconnect_backoff_multiplier,
                        self._options.max_reconnect_delay_ms,
                    )

        self._is_reconnecting = False
        error = ReconnectionError(
            f"Failed to reconnect after {self._options.max_reconnect_attempts} attempts"
        )
        Logger.error(str(error))
        self._emit("error", error)

    async def close(self) -> None:
        """Close the connection."""
        self._is_closing = True

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        if self._connection:
            await self._connection.close()
            self._connection = None

        self._is_connected = False
        self._is_reconnecting = False
        Logger.info("Connection closed")

    async def open_channel(self) -> AbstractChannel:
        """
        Open a new channel.

        Returns:
            A new AMQP channel

        Raises:
            NotConnectedError: If not connected
        """
        if not self._connection or not self._is_connected:
            raise NotConnectedError("Not connected to RabbitMQ")

        return await self._connection.channel()

    async def ensure_exchange(
        self,
        channel: AbstractChannel,
        name: str,
        exchange_type: ExchangeType = ExchangeType.TOPIC,
    ) -> AbstractExchange:
        """
        Ensure an exchange exists.

        Args:
            channel: The channel to use
            name: Exchange name
            exchange_type: Type of exchange

        Returns:
            The exchange object
        """
        return await channel.declare_exchange(
            name, exchange_type, durable=True, auto_delete=False
        )

    async def ensure_queue(
        self,
        channel: AbstractChannel,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> AbstractQueue:
        """
        Ensure a queue exists.

        Args:
            channel: The channel to use
            name: Queue name (empty string for exclusive anonymous queue)
            arguments: Additional queue arguments

        Returns:
            The queue object
        """
        if name:
            return await channel.declare_queue(
                name, durable=True, auto_delete=False, arguments=arguments
            )
        else:
            # Anonymous exclusive queue
            return await channel.declare_queue(
                "", exclusive=True, auto_delete=True, arguments=arguments
            )

    async def bind_queue(
        self,
        queue: AbstractQueue,
        exchange: AbstractExchange,
        routing_key: str,
    ) -> None:
        """
        Bind a queue to an exchange.

        Args:
            queue: The queue to bind
            exchange: The exchange to bind to
            routing_key: Routing key pattern
        """
        await queue.bind(exchange, routing_key)

    async def consume(
        self,
        channel: AbstractChannel,
        queue: AbstractQueue,
        handler: MessageHandler,
        late_ack: bool = False,
        max_concurrent: Optional[int] = None,
        retry_options: Optional[RetryOptions] = None,
    ) -> str:
        """
        Start consuming messages from a queue.

        Args:
            channel: The channel to use
            queue: The queue to consume from
            handler: Message handler function
            late_ack: Whether to use late acknowledgment
            max_concurrent: Maximum concurrent messages (prefetch count)
            retry_options: Options for message retry behavior

        Returns:
            Consumer tag
        """
        if max_concurrent:
            await channel.set_qos(prefetch_count=max_concurrent)

        retry_opts = retry_options or DEFAULT_RETRY_OPTIONS
        queue_name = queue.name

        # The routing key is what RabbitMQ's topic permissions are granted
        # against, and dispatch has to be able to check the message body
        # against it. Handlers written before this took three arguments, and
        # BaseListener.init() accepts caller-supplied ones, so the extra
        # argument is passed only to handlers that declare it.
        wants_routing_key = _handler_accepts_routing_key(handler)

        async def process_message(message: AbstractIncomingMessage) -> None:
            try:
                async with message.process(ignore_processed=True):
                    correlation_id = message.correlation_id or ""
                    incoming_headers = dict(message.headers or {})

                    try:
                        if wants_routing_key:
                            result = await handler(
                                message.body,
                                correlation_id,
                                incoming_headers,
                                message.routing_key,
                            )
                        else:
                            result = await handler(
                                message.body, correlation_id, incoming_headers
                            )

                        # Streaming reply: handler returned an async iterator.
                        # Each yielded chunk is published to reply_to with the
                        # standard streaming headers; the last gets x-protobus-final=true.
                        if hasattr(result, "__aiter__") and not isinstance(
                            result, (bytes, bytearray)
                        ):
                            await self._publish_stream_reply(
                                channel, message, result
                            )
                        # Unary RPC reply
                        elif message.reply_to and result is not None:
                            await channel.default_exchange.publish(
                                Message(
                                    body=result,
                                    correlation_id=message.correlation_id,
                                ),
                                routing_key=message.reply_to,
                            )

                        if late_ack:
                            await message.ack()

                    except Exception as e:
                        import traceback
                        Logger.error(f"Handler error: {type(e).__name__}: {e}")
                        Logger.error(f"Traceback: {traceback.format_exc()}")

                        if is_handled_error(e):
                            # Don't retry handled errors
                            Logger.debug(f"Handled error, not retrying: {e}")
                            if late_ack:
                                await message.ack()
                            return

                        # Check retry count
                        headers = message.headers or {}
                        try:
                            retry_count = int(headers.get("x-retry-count", 0) or 0)
                        except (TypeError, ValueError):
                            retry_count = 0

                        # The handoff must be confirmed BEFORE the delivery is
                        # acked. Acking first — or acking regardless, as this
                        # did — destroys the message when the handoff fails.
                        try:
                            if retry_count < retry_opts.max_retries:
                                await self._retry_message(
                                    channel,
                                    message,
                                    retry_count,
                                    e,
                                    retry_opts,
                                    queue_name,
                                )
                            else:
                                await self._send_to_dlq(channel, message, e)
                        except Exception as handoff_error:
                            Logger.error(
                                f"Retry/DLQ handoff failed for a message on "
                                f"{queue_name}: {handoff_error}. Requeuing "
                                f"rather than dropping it."
                            )
                            if late_ack:
                                await message.nack(requeue=True)
                            return

                        if late_ack:
                            await message.ack()

            except Exception as e:
                Logger.error(f"Error processing message: {e}")

        consumer_tag = await queue.consume(process_message, no_ack=not late_ack)
        return consumer_tag

    async def _publish_stream_reply(
        self,
        channel: AbstractChannel,
        message: AbstractIncomingMessage,
        chunks: Any,
    ) -> None:
        """
        Publish a streaming reply on a single AMQP reply queue.

        Consumes the async iterator `chunks` (each yielding already-encoded
        response bytes), publishing each chunk to ``message.reply_to`` with the
        same ``correlation_id`` as the incoming request. All chunks but the
        last carry ``x-protobus-final=false``; the last carries ``x-protobus-final=true``.

        If the iterator yields no chunks at all, publishes a single empty
        terminal message so the client knows the stream ended.

        See ``docs/advanced/streaming.md`` for the protocol details.
        """
        if not message.reply_to:
            # Nothing to reply to; drain the iterator to clean up.
            try:
                async for _ in chunks:
                    pass
            except Exception:
                pass
            return

        async def _publish_one(body: bytes, seq: int, final: bool) -> None:
            await channel.default_exchange.publish(
                Message(
                    body=body,
                    correlation_id=message.correlation_id,
                    headers={
                        Config.HEADER_FINAL: bool(final),
                        Config.HEADER_SEQ: int(seq),
                    },
                ),
                routing_key=message.reply_to,
            )

        # Look-ahead by one chunk so we can mark the last as final without
        # an extra empty terminal message.
        seq = 0
        buffered: Optional[bytes] = None

        async for chunk in chunks:
            if buffered is not None:
                await _publish_one(buffered, seq=seq, final=False)
                seq += 1
            buffered = chunk

        if buffered is not None:
            await _publish_one(buffered, seq=seq, final=True)
        else:
            # Empty stream — single terminal so the client iterator can end.
            await _publish_one(b"", seq=0, final=True)

    async def _retry_message(
        self,
        channel: AbstractChannel,
        message: AbstractIncomingMessage,
        retry_count: int,
        error: Exception,
        retry_opts: RetryOptions,
        queue_name: str,
    ) -> None:
        """Send a message to the retry queue."""
        headers = dict(message.headers or {})
        headers["x-retry-count"] = retry_count + 1
        headers["x-first-failure-time"] = headers.get(
            "x-first-failure-time", int(time.time() * 1000)
        )
        headers["x-last-error"] = str(error)

        Logger.debug(
            f"Retrying message (attempt {retry_count + 1}/{retry_opts.max_retries})"
        )

        # The retry queue is named after the CONSUMER's queue and is bound to
        # nothing, but this published to the topic bus exchange with the key
        # "<delivery routing key>.retry" — four segments against a three
        # segment binding. Verified against a real broker: the broker returns
        # it as unroutable, aio-pika reports that in the return value rather
        # than by raising, and the message was then acked. Every message that
        # exhausted a handler was silently destroyed on its first retry.
        #
        # Publish to the retry queue by name through the default exchange, the
        # technique _send_to_dlq already used correctly.
        retry_queue_name = f"{queue_name}.retry" if queue_name else None
        if not retry_queue_name:
            Logger.error(
                "Cannot retry: the consumer's queue name is unknown, so the "
                "retry queue cannot be addressed"
            )
            raise UnroutableError("No retry queue for this consumer")

        # Sanitize headers: ensure numeric values are ints (aio-pika strict typing)
        sanitized_headers = {}
        for k, v in headers.items():
            if isinstance(v, str) and v.isdigit():
                sanitized_headers[k] = int(v)
            else:
                sanitized_headers[k] = v

        await publish_confirmed(
            channel.default_exchange,
            Message(
                body=message.body,
                headers=sanitized_headers,
                correlation_id=message.correlation_id,
                reply_to=message.reply_to,
                # SECONDS, as a number. aio-pika's encode_expiration is a
                # singledispatch registered for int, float, timedelta and
                # datetime — and NOT for str, so the previous
                # `str(retry_delay_ms)` raised ValueError at publish time on
                # every aio-pika >= 9. The bare `except` around this block then
                # logged it and the delivery was acked regardless, so every
                # message that hit a non-handled handler error was destroyed
                # rather than retried, on every install.
                expiration=retry_opts.retry_delay_ms / 1000,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            retry_queue_name,
        )

    async def _send_to_dlq(
        self,
        channel: AbstractChannel,
        message: AbstractIncomingMessage,
        error: Exception,
    ) -> None:
        """Send a message to the dead letter queue."""
        headers = dict(message.headers or {})
        headers["x-death-reason"] = str(error)
        headers["x-death-time"] = int(time.time() * 1000)

        dlq_name = f"{message.routing_key}.DLQ"
        Logger.warn(f"Message exhausted retries, sending to DLQ: {dlq_name}")

        # No `except` here: the caller requeues if the handoff fails. Swallowing
        # it meant the delivery was acked immediately afterwards and the message
        # was destroyed rather than dead-lettered.
        await channel.declare_queue(dlq_name, durable=True)
        await publish_confirmed(
            channel.default_exchange,
            Message(
                body=message.body,
                headers=headers,
                correlation_id=message.correlation_id,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            dlq_name,
        )

    async def publish(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        body: bytes,
        properties: Optional[Dict[str, Any]] = None,
        mandatory: bool = True,
    ) -> None:
        """
        Publish a message to an exchange.

        Args:
            channel: The channel to use
            exchange: The exchange to publish to
            routing_key: Message routing key
            body: Message body
            properties: Additional message properties
            mandatory: Fail if nothing is bound for the routing key. True for
                requests; pass False for events, where having no subscribers
                is normal.

        Raises:
            UnroutableError: If mandatory and the broker returned the message.
        """
        if not self._is_connected:
            raise NotConnectedError("Not connected to RabbitMQ")

        props = properties or {}
        message = Message(
            body=body,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            correlation_id=props.get("correlation_id"),
            reply_to=props.get("reply_to"),
            headers=props.get("headers"),
        )

        await publish_confirmed(exchange, message, routing_key, mandatory=mandatory)
