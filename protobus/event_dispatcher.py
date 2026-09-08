"""Publisher for events on the events exchange."""

import uuid
from typing import Any, Optional

from .config import Config
from .connection import IConnection, attach_restorer
from .errors import InvalidMessageError, NotConnectedError
from .logger import Logger
from .message_factory import MessageFactory


class EventDispatcher:
    def __init__(self, connection: IConnection, message_factory: MessageFactory) -> None:
        self._connection = connection
        self._message_factory = message_factory
        self._channel: Any = None
        self._is_initialized = False
        self._bound_on_disconnected = self._on_disconnected
        self._connection.on("disconnected", self._bound_on_disconnected)
        self._detach_restorer = attach_restorer(connection, self._restore, "EventDispatcher")

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def channel(self) -> Any:
        return self._channel

    def _on_disconnected(self, *_args: Any) -> None:
        Logger.debug("EventDispatcher: connection lost, clearing channel")
        self._channel = None

    async def _restore(self, _generation: int = 0) -> None:
        """Reopen the publishing channel. A failure propagates: an event
        dispatcher with no channel silently drops every event."""
        if not self._is_initialized:
            return
        Logger.info("EventDispatcher: reconnected, re-initializing channel")
        self._channel = await self._connection.open_channel()
        Logger.info("EventDispatcher: successfully re-initialized after reconnection")

    async def init(self) -> None:
        if self._is_initialized:
            return
        self._channel = await self._connection.open_channel()
        self._is_initialized = True

    async def publish(self, event_type: str, content: Any, topic: Optional[str] = None) -> None:
        # A reconnection is waited through rather than failed on.
        if not self._connection.is_connected and not self._connection.is_reconnecting:
            raise NotConnectedError("not connected")
        when_ready = getattr(self._connection, "when_ready", None)
        if callable(when_ready):
            await when_ready()
        if not topic:
            topic = f"EVENT.{event_type}"
        properties = {
            "correlation_id": str(uuid.uuid4()),
            "content_type": "application/octet-stream",
            "delivery_mode": 2,
        }
        try:
            event = self._message_factory.build_event(event_type, content, topic)
        except Exception as err:
            # Without the payload — events carry PII too.
            Logger.error(f"failed building event '{event_type}': {err}")
            raise InvalidMessageError(f"failed building event '{event_type}'") from err
        await self._connection.publish(self._channel, Config.events_exchange_name(), topic, event, properties)

    async def close(self) -> None:
        self._connection.off("disconnected", self._bound_on_disconnected)
        self._detach_restorer()
