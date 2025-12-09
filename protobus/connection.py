"""Connection module for RabbitMQ/AMQP connections with automatic reconnection."""

import asyncio
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
    is_handled_error,
)
from .logger import Logger

# Type aliases
MessageHandler = Callable[[bytes, str], Awaitable[Optional[bytes]]]


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

        self._url = url
        Logger.info(f"Connecting to bus: {url}")

        try:
            self._connection = await aio_pika.connect_robust(
                url,
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

        async def process_message(message: AbstractIncomingMessage) -> None:
            try:
                async with message.process(ignore_processed=True):
                    correlation_id = message.correlation_id or ""

                    try:
                        result = await handler(message.body, correlation_id)

                        # Handle RPC reply
                        if message.reply_to and result is not None:
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
                        if is_handled_error(e):
                            # Don't retry handled errors
                            Logger.debug(f"Handled error, not retrying: {e}")
                            if late_ack:
                                await message.ack()
                            return

                        # Check retry count
                        headers = message.headers or {}
                        retry_count = headers.get("x-retry-count", 0)

                        if retry_count < retry_opts.max_retries:
                            # Retry the message
                            await self._retry_message(
                                channel, message, retry_count, e, retry_opts
                            )
                        else:
                            # Send to DLQ
                            await self._send_to_dlq(channel, message, e)

                        if late_ack:
                            await message.ack()

            except Exception as e:
                Logger.error(f"Error processing message: {e}")

        consumer_tag = await queue.consume(process_message, no_ack=not late_ack)
        return consumer_tag

    async def _retry_message(
        self,
        channel: AbstractChannel,
        message: AbstractIncomingMessage,
        retry_count: int,
        error: Exception,
        retry_opts: RetryOptions,
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

        # Re-publish with delay (using message TTL on retry queue)
        retry_queue_name = f"{message.routing_key}.retry"
        try:
            retry_exchange = await channel.get_exchange(Config.bus_exchange_name())
            await retry_exchange.publish(
                Message(
                    body=message.body,
                    headers=headers,
                    correlation_id=message.correlation_id,
                    reply_to=message.reply_to,
                    expiration=str(retry_opts.retry_delay_ms),
                ),
                routing_key=retry_queue_name,
            )
        except Exception as e:
            Logger.error(f"Failed to retry message: {e}")

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

        try:
            dlq = await channel.declare_queue(dlq_name, durable=True)
            await channel.default_exchange.publish(
                Message(
                    body=message.body,
                    headers=headers,
                    correlation_id=message.correlation_id,
                ),
                routing_key=dlq_name,
            )
        except Exception as e:
            Logger.error(f"Failed to send to DLQ: {e}")

    async def publish(
        self,
        channel: AbstractChannel,
        exchange: AbstractExchange,
        routing_key: str,
        body: bytes,
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Publish a message to an exchange.

        Args:
            channel: The channel to use
            exchange: The exchange to publish to
            routing_key: Message routing key
            body: Message body
            properties: Additional message properties
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

        await exchange.publish(message, routing_key=routing_key)
