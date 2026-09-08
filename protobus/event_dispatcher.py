"""Publisher for events on the events exchange."""

import uuid
from typing import Any, Optional

from .config import Config
from .connection import EXCHANGE_OPTIONS, IConnection, attach_restorer
from .errors import ChannelClosedError, InvalidMessageError, NotConnectedError
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
        await self._open()
        Logger.info("EventDispatcher: successfully re-initialized after reconnection")

    async def _open(self) -> None:
        """
        Open the publishing channel and declare the events exchange on it.

        A publisher must not depend on a subscriber having started first: an
        event with no subscribers is a normal outcome, but an event published
        to an exchange nobody has declared is a channel-closing NOT_FOUND.
        """
        channel = await self._connection.open_channel()
        await self._connection.declare_exchange(channel, Config.events_exchange_name(), "topic", dict(EXCHANGE_OPTIONS))
        self._channel = channel

    async def init(self) -> None:
        if self._is_initialized:
            return
        await self._open()
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
            # Fixed before the first attempt, so a republish after a lost
            # channel is recognisably the same event.
            "message_id": str(uuid.uuid4()),
        }
        try:
            event = self._message_factory.build_event(event_type, content, topic)
        except Exception as err:
            # Without the payload — events carry PII too.
            Logger.error(f"failed building event '{event_type}': {err}")
            raise InvalidMessageError(f"failed building event '{event_type}'") from err
        try:
            await self._connection.publish(self._channel, Config.events_exchange_name(), topic, event, properties)
        except ChannelClosedError:
            # The socket died underneath the publish: wait for the restored
            # channel and publish once more under the same message id.
            if self._connection.is_connected and not self._connection.is_reconnecting:
                raise
            if callable(when_ready):
                await when_ready()
            await self._connection.publish(self._channel, Config.events_exchange_name(), topic, event, properties)

    async def close(self) -> None:
        self._connection.off("disconnected", self._bound_on_disconnected)
        self._detach_restorer()
