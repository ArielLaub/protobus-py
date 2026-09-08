"""Context: one connection, one schema, one publisher of each kind."""

from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Union, runtime_checkable

from .connection import Connection, ReconnectionOptions
from .event_dispatcher import EventDispatcher
from .logger import Logger
from .message_dispatcher import CallOptions, MessageDispatcher, StreamingReply, StreamOptions
from .message_factory import MessageFactory


@dataclass
class ContextOptions:
    reconnection: Optional[ReconnectionOptions] = None


@runtime_checkable
class IContext(Protocol):
    """What a service or proxy needs from its context."""

    @property
    def factory(self) -> MessageFactory: ...

    @property
    def connection(self) -> Any: ...

    @property
    def is_connected(self) -> bool: ...

    @property
    def is_reconnecting(self) -> bool: ...

    async def publish_message(
        self, content: bytes, routing_key: str, rpc: bool = True,
        timeout_ms: Optional[int] = None, options: Optional[CallOptions] = None,
    ) -> Optional[bytes]: ...

    def publish_streaming_message(
        self, content: bytes, routing_key: str, idle_timeout_ms: Optional[int] = None,
        options: Optional[StreamOptions] = None,
    ) -> Any: ...

    async def publish_event(self, event_type: str, content: Any, topic: Optional[str] = None) -> None: ...


class Context:
    def __init__(self, connection: Optional[Connection] = None) -> None:
        self._connection = connection or Connection()
        self._message_factory = MessageFactory()
        self._message_dispatcher = MessageDispatcher(self._connection)
        self._event_dispatcher = EventDispatcher(self._connection, self._message_factory)
        self._is_closed = False

        self._connection.on("reconnecting", lambda info: Logger.info(
            f"Context: reconnecting (attempt {info.get('attempt')}, delay {info.get('delay')}ms)"
        ))
        self._connection.on("reconnected", lambda *_: Logger.info("Context: reconnected successfully"))
        self._connection.on("disconnected", lambda *_: Logger.warn("Context: connection lost"))
        self._connection.on("error", lambda err: Logger.error(f"Context: connection error - {err}"))

    async def init(
        self,
        amqp_connection_string: str,
        proto_locations: Optional[Union[str, List[str]]] = None,
        options: Optional[ContextOptions] = None,
        *,
        proto_dirs: Optional[Union[str, List[str]]] = None,
    ) -> None:
        """
        Load the schema from ``proto_locations`` (directories or files,
        searched recursively), connect, and open the publishing channels.
        ``proto_dirs`` is the 1.x keyword for the same argument.
        """
        self._message_factory.init(proto_locations or proto_dirs or [])
        await self._connection.connect(amqp_connection_string, options.reconnection if options else None)
        await self._message_dispatcher.init()
        await self._event_dispatcher.init()

    async def close(self) -> None:
        """Release the dispatchers and disconnect."""
        if self._is_closed:
            return
        self._is_closed = True
        try:
            await self._message_dispatcher.close()
        except Exception as err:
            Logger.debug(f"Context: error closing message dispatcher: {err}")
        try:
            await self._event_dispatcher.close()
        except Exception as err:
            Logger.debug(f"Context: error closing event dispatcher: {err}")
        await self._connection.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._connection.is_connected

    @property
    def is_reconnecting(self) -> bool:
        return self._connection.is_reconnecting

    @property
    def factory(self) -> MessageFactory:
        return self._message_factory

    @property
    def connection(self) -> Connection:
        return self._connection

    @property
    def message_dispatcher(self) -> MessageDispatcher:
        return self._message_dispatcher

    @property
    def event_dispatcher(self) -> EventDispatcher:
        return self._event_dispatcher

    async def publish_message(
        self,
        content: bytes,
        routing_key: str,
        rpc: bool = True,
        timeout_ms: Optional[int] = None,
        options: Optional[CallOptions] = None,
        priority: Optional[int] = None,
    ) -> Optional[bytes]:
        return await self._message_dispatcher.publish(content, routing_key, rpc is not False, timeout_ms, options, priority)

    def publish_streaming_message(
        self,
        content: bytes,
        routing_key: str,
        idle_timeout_ms: Optional[int] = None,
        options: Optional[StreamOptions] = None,
    ) -> StreamingReply:
        return self._message_dispatcher.publish_streaming(content, routing_key, idle_timeout_ms, options)

    async def publish_event(self, event_type: str, content: Any, topic: Optional[str] = None) -> None:
        await self._event_dispatcher.publish(event_type, content, topic)
