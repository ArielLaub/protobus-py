"""Base listener module for message queue consumption."""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aio_pika import ExchangeType
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractQueue

from .config import Config
from .connection import Connection, IConnection, RetryOptions
from .errors import (
    AlreadyStartedError,
    MissingExchangeError,
    NotConnectedError,
    NotInitializedError,
)
from .logger import Logger

# Type alias for message handlers
MessageHandler = Callable[[bytes, str], Awaitable[Optional[bytes]]]


class BaseListener:
    """
    Base class for message queue listeners.

    Handles queue initialization, message consumption, and connection lifecycle.
    Supports automatic reconnection and binding restoration.
    """

    def __init__(
        self,
        connection: IConnection,
        late_ack: bool = False,
        max_concurrent: Optional[int] = None,
        message_ttl_ms: Optional[int] = None,
    ):
        """
        Initialize the base listener.

        Args:
            connection: The connection to use
            late_ack: Whether to use late acknowledgment
            max_concurrent: Maximum concurrent messages (prefetch count)
            message_ttl_ms: Optional message TTL in milliseconds
        """
        self._connection = connection
        self._late_ack = late_ack
        self._max_concurrent = max_concurrent
        self._message_ttl_ms = message_ttl_ms

        self._channel: Optional[AbstractChannel] = None
        self._exchange: Optional[AbstractExchange] = None
        self._queue: Optional[AbstractQueue] = None
        self._queue_name: str = ""
        self._exchange_name: str = ""
        self._exchange_type: ExchangeType = ExchangeType.TOPIC

        self._bindings: List[str] = []
        self._handler: Optional[MessageHandler] = None
        self._consumer_tag: Optional[str] = None

        self._is_initialized = False
        self._was_started = False

        # Set up connection event handlers
        self._connection.on("reconnected", self._on_reconnected)
        self._connection.on("disconnected", self._on_disconnected)

    @property
    def queue_name(self) -> str:
        """Get the queue name."""
        return self._queue_name

    @property
    def exchange_name(self) -> str:
        """Get the exchange name."""
        return self._exchange_name

    @property
    def is_initialized(self) -> bool:
        """Check if the listener has been initialized."""
        return self._is_initialized

    @property
    def was_started(self) -> bool:
        """Check if the listener was ever started."""
        return self._was_started

    async def init(
        self,
        handler: MessageHandler,
        queue_name: str = "",
    ) -> None:
        """
        Initialize the listener.

        Args:
            handler: Message handler function
            queue_name: Queue name (empty for anonymous queue)
        """
        if not self._exchange_name:
            raise MissingExchangeError("Exchange name not set")

        self._handler = handler
        self._queue_name = queue_name

        await self._setup_channel()
        self._is_initialized = True

    async def _setup_channel(self) -> None:
        """Set up the channel, exchange, and queue."""
        self._channel = await self._connection.open_channel()

        # Declare exchange
        self._exchange = await self._connection.ensure_exchange(
            self._channel,
            self._exchange_name,
            self._exchange_type,
        )

        # Prepare queue arguments
        arguments: Dict[str, Any] = {}
        if self._message_ttl_ms is not None:
            arguments["x-message-ttl"] = self._message_ttl_ms

        # Declare queue
        self._queue = await self._connection.ensure_queue(
            self._channel,
            self._queue_name,
            arguments=arguments if arguments else None,
        )

        # Update queue name for anonymous queues
        if not self._queue_name and self._queue:
            self._queue_name = self._queue.name

        Logger.debug(f"Set up channel for queue: {self._queue_name}")

    async def subscribe(self, routing_key: str) -> None:
        """
        Subscribe to messages matching a routing key pattern.

        Args:
            routing_key: Routing key pattern to subscribe to
        """
        if not self._is_initialized:
            raise NotInitializedError("Listener not initialized")

        if not self._queue or not self._exchange:
            raise NotConnectedError("Queue or exchange not available")

        await self._connection.bind_queue(self._queue, self._exchange, routing_key)
        self._bindings.append(routing_key)
        Logger.debug(f"Subscribed to: {routing_key}")

    async def start(self) -> None:
        """Start consuming messages."""
        if self._was_started:
            raise AlreadyStartedError("Listener already started")

        if not self._is_initialized:
            raise NotInitializedError("Listener not initialized")

        if not self._handler:
            raise NotInitializedError("No handler set")

        if not self._channel or not self._queue:
            raise NotConnectedError("Channel or queue not available")

        self._consumer_tag = await self._connection.consume(
            self._channel,
            self._queue,
            self._handler,
            late_ack=self._late_ack,
            max_concurrent=self._max_concurrent,
        )
        self._was_started = True
        Logger.debug(f"Started consuming from: {self._queue_name}")

    async def close(self) -> None:
        """Stop consuming and close the channel."""
        if self._consumer_tag and self._queue:
            try:
                await self._queue.cancel(self._consumer_tag)
            except Exception as e:
                Logger.warn(f"Error cancelling consumer: {e}")

        if self._channel:
            try:
                await self._channel.close()
            except Exception as e:
                Logger.warn(f"Error closing channel: {e}")

        self._channel = None
        self._queue = None
        self._exchange = None
        self._consumer_tag = None
        Logger.debug(f"Closed listener for queue: {self._queue_name}")

    async def _on_reconnected(self) -> None:
        """Handle reconnection event."""
        Logger.debug(f"Reconnected, reinitializing listener: {self._queue_name}")

        try:
            await self._setup_channel()

            # Rebind all routing keys
            if self._queue and self._exchange:
                for routing_key in self._bindings:
                    await self._connection.bind_queue(
                        self._queue, self._exchange, routing_key
                    )
                    Logger.debug(f"Rebound: {routing_key}")

            # Resume consumption if we were started
            if self._was_started and self._handler and self._channel and self._queue:
                self._consumer_tag = await self._connection.consume(
                    self._channel,
                    self._queue,
                    self._handler,
                    late_ack=self._late_ack,
                    max_concurrent=self._max_concurrent,
                )
                Logger.debug(f"Resumed consuming: {self._queue_name}")

        except Exception as e:
            Logger.error(f"Error during reconnection: {e}")

    def _on_disconnected(self) -> None:
        """Handle disconnection event."""
        Logger.debug(f"Disconnected, clearing listener state: {self._queue_name}")
        self._channel = None
        self._queue = None
        self._exchange = None
        self._consumer_tag = None
