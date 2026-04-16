"""Context module - central coordinator for Protobus messaging."""

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .connection import Connection, ConnectionOptions, IConnection
from .event_dispatcher import EventDispatcher
from .logger import Logger
from .message_dispatcher import MessageDispatcher
from .message_factory import MessageFactory


@dataclass
class ContextOptions:
    """Options for context initialization."""

    max_reconnect_attempts: int = 10
    initial_reconnect_delay_ms: int = 1000
    max_reconnect_delay_ms: int = 30000
    reconnect_backoff_multiplier: float = 2.0


class IContext(Protocol):
    """Interface for context implementations."""

    @property
    def is_connected(self) -> bool:
        ...

    @property
    def is_reconnecting(self) -> bool:
        ...

    @property
    def factory(self) -> MessageFactory:
        ...

    @property
    def connection(self) -> IConnection:
        ...

    async def init(self, url: str) -> None:
        ...

    async def close(self) -> None:
        ...

    async def publish_message(
        self, data: bytes, routing_key: str, rpc: bool = True
    ) -> Optional[bytes]:
        ...

    async def publish_event(
        self, event_type: str, data: Any, topic: Optional[str] = None
    ) -> None:
        ...


class Context:
    """
    Central coordinator for Protobus messaging infrastructure.

    Provides a facade over the connection, message factory, and dispatchers.
    """

    def __init__(self, options: Optional[ContextOptions] = None):
        """
        Initialize the context.

        Args:
            options: Optional configuration options
        """
        opts = options or ContextOptions()

        # Create connection with options
        connection_options = ConnectionOptions(
            max_reconnect_attempts=opts.max_reconnect_attempts,
            initial_reconnect_delay_ms=opts.initial_reconnect_delay_ms,
            max_reconnect_delay_ms=opts.max_reconnect_delay_ms,
            reconnect_backoff_multiplier=opts.reconnect_backoff_multiplier,
        )
        self._connection = Connection(connection_options)
        self._factory = MessageFactory()
        self._message_dispatcher: Optional[MessageDispatcher] = None
        self._event_dispatcher: Optional[EventDispatcher] = None

        # Forward connection events
        self._connection.on("reconnecting", self._on_reconnecting)
        self._connection.on("reconnected", self._on_reconnected)
        self._connection.on("disconnected", self._on_disconnected)
        self._connection.on("error", self._on_error)

    @property
    def is_connected(self) -> bool:
        """Check if connected to RabbitMQ."""
        return self._connection.is_connected

    @property
    def is_reconnecting(self) -> bool:
        """Check if currently reconnecting."""
        return self._connection.is_reconnecting

    @property
    def factory(self) -> MessageFactory:
        """Get the message factory."""
        return self._factory

    @property
    def connection(self) -> IConnection:
        """Get the connection."""
        return self._connection

    async def init(self, url: str, proto_dirs: Optional[list] = None) -> None:
        """
        Initialize the context and connect to RabbitMQ.

        Args:
            url: AMQP connection URL
            proto_dirs: Optional list of directories containing .proto files
                        for binary protobuf encoding/decoding of inner messages
        """
        # Initialize the message factory
        await self._factory.init(root_paths=proto_dirs)

        # Connect to RabbitMQ
        await self._connection.connect(url)

        # Initialize dispatchers
        self._message_dispatcher = MessageDispatcher(self._connection)
        await self._message_dispatcher.init()

        self._event_dispatcher = EventDispatcher(self._connection, self._factory)
        await self._event_dispatcher.init()

        Logger.info("Context initialized")

    async def close(self) -> None:
        """Close the context and connection."""
        await self._connection.close()
        Logger.info("Context closed")

    async def publish_message(
        self,
        data: bytes,
        routing_key: str,
        rpc: bool = True,
    ) -> Optional[bytes]:
        """
        Publish a message, optionally waiting for a response.

        Args:
            data: Message data
            routing_key: Routing key for the message
            rpc: Whether to wait for a response

        Returns:
            Response data if rpc=True, None otherwise
        """
        if not self._message_dispatcher:
            raise RuntimeError("Context not initialized")

        return await self._message_dispatcher.publish(data, routing_key, rpc)

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
        if not self._event_dispatcher:
            raise RuntimeError("Context not initialized")

        await self._event_dispatcher.publish(event_type, data, topic)

    def _on_reconnecting(self, attempt: int, max_attempts: int) -> None:
        """Handle reconnecting event."""
        Logger.info(f"Reconnecting to RabbitMQ (attempt {attempt}/{max_attempts})")

    def _on_reconnected(self) -> None:
        """Handle reconnected event."""
        Logger.info("Reconnected to RabbitMQ")

    def _on_disconnected(self) -> None:
        """Handle disconnected event."""
        Logger.warn("Disconnected from RabbitMQ")

    def _on_error(self, error: Exception) -> None:
        """Handle error event."""
        Logger.error(f"Connection error: {error}")
