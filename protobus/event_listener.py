"""Event listener with topic-based routing and handler registration."""

from typing import Any, Awaitable, Callable, Optional

from aio_pika import ExchangeType

from .base_listener import BaseListener
from .config import Config
from .connection import IConnection
from .errors import NotInitializedError
from .logger import Logger
from .message_factory import MessageFactory
from .trie import Trie

# Type alias for event handlers
EventHandler = Callable[[Any, str], Awaitable[None]]


class EventListener(BaseListener):
    """
    Listener for event messages with topic-based routing.

    Uses a Trie data structure to match topics to handlers.
    Supports wildcard patterns (* for single level, # for multi-level).
    """

    def __init__(
        self,
        connection: IConnection,
        message_factory: MessageFactory,
    ):
        """
        Initialize the event listener.

        Args:
            connection: The connection to use
            message_factory: Factory for decoding event messages
        """
        super().__init__(connection, late_ack=True, max_concurrent=None)
        self._exchange_name = Config.events_exchange_name()
        self._exchange_type = ExchangeType.TOPIC
        self._message_factory = message_factory
        self._router: Trie = Trie()
        self._all_handler: Optional[EventHandler] = None

    async def init(
        self,
        handler: Optional[Callable[[bytes, str], Awaitable[Optional[bytes]]]] = None,
        queue_name: str = "",
    ) -> None:
        """
        Initialize the event listener.

        Args:
            handler: Optional default handler (typically not used for events)
            queue_name: Queue name (empty for anonymous queue)
        """
        # Create our own handler that routes to registered event handlers
        async def event_handler(data: bytes, correlation_id: str) -> Optional[bytes]:
            try:
                event = self._message_factory.decode_event(data)
                event_type = event.type
                event_data = event.data
                # Use full routing key format for matching
                topic = event.topic or f"EVENT.{event_type}"

                # Call the all-events handler if registered
                if self._all_handler:
                    try:
                        await self._all_handler(event_data, topic)
                    except Exception as e:
                        Logger.error(f"Error in all-events handler: {e}")

                # Find matching handlers using the trie
                handlers = self._router.match_topic(topic)
                if handlers:
                    for h in handlers:
                        try:
                            await h(event_data, topic)
                        except Exception as e:
                            Logger.error(f"Error in event handler for {topic}: {e}")
                elif not self._all_handler:
                    Logger.warn(f"No handler for event topic: {topic}")

            except Exception as e:
                Logger.error(f"Error processing event: {e}")

            return None

        await super().init(event_handler, queue_name)

    async def subscribe(
        self,
        event_type: str,
        handler: EventHandler,
        topic: Optional[str] = None,
    ) -> None:
        """
        Subscribe to events of a specific type.

        Args:
            event_type: Event type to subscribe to
            handler: Handler function for the events
            topic: Optional custom topic pattern (defaults to EVENT.<event_type>)
        """
        if not self._is_initialized:
            raise NotInitializedError("EventListener not initialized")

        # Determine the routing key
        routing_key = topic or f"EVENT.{event_type}"

        # Register the handler in the trie
        self._router.add_match(routing_key, handler)

        # Bind to the exchange
        await super().subscribe(routing_key)
        Logger.debug(f"Subscribed to event: {routing_key}")

    async def subscribe_all(self, handler: EventHandler) -> None:
        """
        Subscribe to all events.

        Args:
            handler: Handler function for all events
        """
        if not self._is_initialized:
            raise NotInitializedError("EventListener not initialized")

        self._all_handler = handler

        # Bind to all events using wildcard
        await super().subscribe("#")
        Logger.debug("Subscribed to all events")

    # Note: unsubscribe is not implemented as the trie doesn't support removal
    # This matches the TypeScript implementation's limitation
