"""Event dispatcher for publishing events."""

import asyncio
import uuid
from typing import Any, Optional

from aio_pika import ExchangeType, Message
from aio_pika.abc import AbstractChannel, AbstractExchange

from .config import Config
from .connection import (
    IConnection,
    detach_listener,
    release_amqp_resources,
    schedule_amqp_release,
)
from .errors import InvalidMessageError, NotConnectedError, NotInitializedError
from .logger import Logger
from .message_factory import MessageFactory


class EventDispatcher:
    """
    Dispatcher for publishing events to the event exchange.

    Handles event serialization and routing.
    """

    def __init__(self, connection: IConnection, message_factory: MessageFactory):
        """
        Initialize the event dispatcher.

        Args:
            connection: The connection to use
            message_factory: Factory for building event messages
        """
        self._connection = connection
        self._message_factory = message_factory
        self._channel: Optional[AbstractChannel] = None
        self._exchange: Optional[AbstractExchange] = None
        self._is_initialized = False
        self._is_closed = False

        # Serialises channel setup. A flapping broker can deliver a second
        # 'reconnected' while the first re-setup is still awaiting a round-trip,
        # and _emit dispatches handlers with create_task, so the two would
        # otherwise interleave and each leave the other's channel and consumer
        # behind. _setup_channel/_teardown_channel assume the caller holds this.
        self._setup_lock = asyncio.Lock()

        # Set up connection event handlers. Bound refs are stored so close()
        # can unregister exactly these callbacks (TS parity).
        self._bound_on_reconnected = self._on_reconnected
        self._bound_on_disconnected = self._on_disconnected
        self._connection.on("reconnected", self._bound_on_reconnected)
        self._connection.on("disconnected", self._bound_on_disconnected)

    @property
    def is_initialized(self) -> bool:
        """Check if the dispatcher has been initialized."""
        return self._is_initialized

    async def init(self) -> None:
        """Initialize the event dispatcher."""
        async with self._setup_lock:
            if self._is_initialized:
                return
            await self._setup_channel()
            self._is_initialized = True

    async def _setup_channel(self) -> None:
        """
        Set up the channel and exchange.

        Also called on reconnect, so the previous channel is released first —
        overwriting the reference left it open on the broker forever.
        """
        await self._teardown_channel()

        self._channel = await self._connection.open_channel()

        # Declare the events exchange
        self._exchange = await self._connection.ensure_exchange(
            self._channel,
            Config.events_exchange_name(),
            ExchangeType.TOPIC,
        )

        Logger.debug("EventDispatcher initialized")

    async def _teardown_channel(self) -> None:
        """Best-effort release of the current channel."""
        channel = self._channel
        self._channel = None
        self._exchange = None
        await release_amqp_resources(channel)

    async def close(self) -> None:
        """Detach from the connection and release the channel."""
        if self._is_closed:
            return
        self._is_closed = True

        detach_listener(self._connection, "reconnected", self._bound_on_reconnected)
        detach_listener(
            self._connection, "disconnected", self._bound_on_disconnected
        )

        async with self._setup_lock:
            await self._teardown_channel()
        Logger.debug("EventDispatcher closed")

    async def publish(
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
            topic: Optional custom topic (defaults to EVENT.<event_type>)

        Raises:
            NotConnectedError: If not connected
            NotInitializedError: If not initialized
            InvalidMessageError: If message building fails
        """
        if not self._is_initialized:
            raise NotInitializedError("EventDispatcher not initialized")

        if not self._connection.is_connected:
            raise NotConnectedError("Not connected to RabbitMQ")

        if not self._channel or not self._exchange:
            raise NotConnectedError("Channel or exchange not available")

        # Determine routing key
        routing_key = topic or f"EVENT.{event_type}"

        try:
            # Build the event message
            body = self._message_factory.build_event(event_type, data, topic)
        except Exception as e:
            Logger.error(f"Failed to build event message: {e}")
            raise InvalidMessageError(f"Failed to build event: {e}")

        correlation_id = str(uuid.uuid4())

        message = Message(
            body=body,
            correlation_id=correlation_id,
        )

        await self._exchange.publish(message, routing_key=routing_key)
        Logger.debug(f"Published event {event_type} to {routing_key}")

    async def _on_reconnected(self) -> None:
        """Handle reconnection event."""
        if self._is_initialized and not self._is_closed:
            Logger.debug("EventDispatcher reconnecting...")
            async with self._setup_lock:
                if self._is_closed:
                    return
                try:
                    await self._setup_channel()
                    Logger.debug("EventDispatcher reconnected")
                except Exception as e:
                    Logger.error(f"Error reconnecting EventDispatcher: {e}")

    def _on_disconnected(self) -> None:
        """Handle disconnection event."""
        Logger.debug("EventDispatcher disconnected")

        # Release rather than merely forget: an event delivered while the
        # connection is still up would otherwise strand a live channel.
        channel = self._channel
        self._channel = None
        self._exchange = None
        schedule_amqp_release(channel)
