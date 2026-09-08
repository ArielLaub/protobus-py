"""Base listener: a channel, an exchange, a queue, its bindings and a consumer,
with restoration after reconnection."""

import uuid
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .connection import ConsumeOptions, ConsumeRetryOptions, IConnection, MessageHandler, attach_restorer
from .errors import (
    AlreadyStartedError,
    ConnectionError,
    MissingExchangeError,
    NotConnectedError,
    NotInitializedError,
)
from .events import EventEmitter
from .logger import Logger


class BaseListener(EventEmitter):
    """
    Common lifecycle for everything that consumes a queue.

    Events: 'initialized', 'started', 'disconnected', 'reconnected'.
    """

    def __init__(self, connection: IConnection) -> None:
        super().__init__()
        self._connection = connection
        self._queue_name = ""
        # The name asked for at init(). An anonymous queue's real name is
        # broker-generated and must not be re-declared after a reconnect.
        self._configured_queue_name = ""
        self._exchange_name = ""
        self._exchange_type = ""
        self._channel: Any = None
        self._consumer_tag = ""
        self._handler: Optional[MessageHandler] = None
        self._is_anonymous = True
        self._late_ack = False
        self._max_concurrent: Optional[int] = None  # only used for late-ack workers
        self._message_ttl_ms: Optional[int] = None
        self._max_priority: Optional[int] = None
        self._processing_timeout_ms: Optional[int] = None
        self._bindings: List[str] = []  # bound routing keys, for reconnection
        self._is_initialized = False
        self._was_started = False
        self._detach_restorer: Callable[[], None] = lambda: None
        self._restorer_attached = False

        async def default_handler(message: bytes, correlation_id: str = "", *_args: Any) -> None:
            # Size and correlation id only. Never the body.
            Logger.warn(
                "unhandled message by default handler "
                f"({len(message) if message else 0} bytes, correlationId {correlation_id or 'none'})"
            )

        self._default_handler: MessageHandler = default_handler

        # Restoration is coordinated by the connection, which waits for it
        # before reporting itself reconnected. Disconnection stays an event.
        self._attach_restorer()
        self._bound_on_disconnected = self._on_disconnected
        self._connection.on("disconnected", self._bound_on_disconnected)

    # -- properties -----------------------------------------------------------

    @property
    def connection(self) -> IConnection:
        return self._connection

    @property
    def queue_name(self) -> str:
        return self._queue_name

    @property
    def exchange_name(self) -> str:
        return self._exchange_name

    @property
    def channel(self) -> Any:
        return self._channel

    @property
    def consumer_tag(self) -> str:
        return self._consumer_tag

    @property
    def bindings(self) -> List[str]:
        return list(self._bindings)

    @property
    def is_connected(self) -> bool:
        return self._connection.is_connected

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def was_started(self) -> bool:
        return self._was_started

    @property
    def is_anonymous(self) -> bool:
        return self._is_anonymous

    @property
    def is_ready(self) -> bool:
        """Whether the listener currently holds a usable channel and queue."""
        return self._channel is not None and bool(self._queue_name)

    # -- restoration ----------------------------------------------------------

    def _attach_restorer(self) -> None:
        """
        Take part in the connection's restoration. Idempotent, paired with
        ``_detach_restorer``: a listener attaches on construction and again
        on every start(), and detaches on stop_consuming() and close().
        """
        if self._restorer_attached:
            return
        detach = attach_restorer(self._connection, self._restore, type(self).__name__)
        self._restorer_attached = True

        def detach_once() -> None:
            if not self._restorer_attached:
                return
            self._restorer_attached = False
            detach()

        self._detach_restorer = detach_once

    def _on_disconnected(self, *_args: Any) -> None:
        Logger.debug(f"{type(self).__name__}: connection lost, clearing channel state")
        self._channel = None
        self._consumer_tag = ""
        self.emit("disconnected")

    async def _restore(self, _generation: int = 0) -> None:
        """
        Put this listener's channel, queue, bindings and consumer back. A
        failure propagates to the connection, which treats the whole
        generation as unusable and retries.
        """
        if not self._is_initialized:
            return
        Logger.info(f"{type(self).__name__}: reconnected, re-initializing...")
        await self._reinitialize()
        for routing_key in self._bindings:
            await self._connection.bind_queue(self._channel, self._queue_name, self._exchange_name, routing_key, {})
            Logger.debug(f"{type(self).__name__}: re-bound {routing_key}")
        if self._was_started:
            await self._start_consuming()
        Logger.info(f"{type(self).__name__}: successfully re-initialized after reconnection")
        self.emit("reconnected")

    # 1.x name.
    restore = _restore

    async def _reinitialize(self) -> None:
        """Re-create channel, exchange and queue without changing configuration."""
        self._channel = await self._connection.open_channel()
        if self._late_ack:
            await self._apply_prefetch()
        await self._connection.declare_exchange(
            self._channel, self._exchange_name, self._exchange_type,
            {"auto_delete": False, "durable": True, "internal": False, "arguments": {}},
        )
        queue_name_to_use = "" if self._is_anonymous else self._configured_queue_name
        self._queue_name = await self._connection.declare_queue(
            self._channel, queue_name_to_use,
            {
                "auto_delete": self._is_anonymous,
                "durable": not self._is_anonymous,
                "exclusive": self._is_anonymous,
                "arguments": self.build_queue_arguments(),
            },
        )
        if self._exchange_type == "direct":
            await self._connection.bind_queue(self._channel, self._queue_name, self._exchange_name, self._queue_name, {})

    async def _apply_prefetch(self) -> None:
        """Bound how many unacked deliveries the broker pushes at once."""
        set_prefetch = getattr(self._connection, "set_prefetch", None)
        if callable(set_prefetch):
            await set_prefetch(self._channel, self.effective_prefetch())
        else:
            await self._channel.basic_qos(prefetch_count=self.effective_prefetch())

    async def _start_consuming(self) -> None:
        self._consumer_tag = str(uuid.uuid4())
        options = ConsumeOptions(consumer_tag=self._consumer_tag, no_ack=False, exclusive=self._is_anonymous)
        assert self._handler is not None
        await self._connection.consume(
            self._channel, self._queue_name, self._handler, options,
            self._late_ack, self.get_retry_options(), self._processing_timeout_ms,
        )
        Logger.debug(f"{type(self).__name__}: started consuming from {self._queue_name}")

    def get_retry_options(self) -> Optional[ConsumeRetryOptions]:
        """Override in subclasses to enable retry."""
        return None

    # -- lifecycle ------------------------------------------------------------

    async def init(self, message_handler: Optional[MessageHandler], queue_name: str = "") -> None:
        if self._is_initialized:
            return
        if not self._exchange_name:
            raise MissingExchangeError("listener has no exchange name")
        if not self._connection.is_connected:
            raise ConnectionError("connection is not up")

        self._handler = message_handler or self._default_handler
        self._is_anonymous = not queue_name
        self._configured_queue_name = queue_name or ""

        self._channel = await self._connection.open_channel()
        if self._late_ack:
            await self._apply_prefetch()
        await self._connection.declare_exchange(
            self._channel, self._exchange_name, self._exchange_type,
            {"auto_delete": False, "durable": True, "internal": False, "arguments": {}},
        )
        self._queue_name = await self._connection.declare_queue(
            self._channel, queue_name or "",
            {
                "auto_delete": self._is_anonymous,
                "durable": not self._is_anonymous,
                "exclusive": self._is_anonymous,
                "arguments": self.build_queue_arguments(),
            },
        )
        if self._exchange_type == "direct":
            await self._connection.bind_queue(self._channel, self._queue_name, self._exchange_name, self._queue_name, {})

        self._is_initialized = True
        self.emit("initialized", {})

    async def start(self) -> None:
        if not self._is_initialized:
            raise NotInitializedError("listener not initialized")
        if self._was_started and self._consumer_tag:
            raise AlreadyStartedError("listener already started")
        if not self._connection.is_connected:
            raise NotConnectedError("connection is not up")
        # Restored again from here on: stop_consuming() drops out of
        # restoration, and resuming has to undo that.
        self._attach_restorer()
        await self._start_consuming()
        self._was_started = True
        self.emit("started", {})

    async def stop_consuming(self) -> None:
        """
        Stop accepting NEW deliveries while leaving the channel open, so
        handlers still running can ack and publish their replies. The first
        step of a graceful shutdown. Safe to call more than once.
        """
        tag = self._consumer_tag
        # Recorded first and unconditionally: a consumer tag is not evidence
        # there is anything to stop, and a reconnection inside the drain
        # window must not put the consumer back in a process shutting down.
        self._consumer_tag = ""
        self._was_started = False
        self._detach_restorer()
        if not tag:
            return
        if not self._connection.is_connected or self._channel is None:
            return
        try:
            await self._connection.cancel(self._channel, tag)
            Logger.debug(f"{type(self).__name__}: stopped consuming ({tag})")
        except Exception as err:
            Logger.debug(f"{type(self).__name__}: failed to cancel consumer '{tag}' during drain: {err}")

    async def close(self) -> None:
        if not self._is_initialized:
            raise NotInitializedError("listener not initialized")
        self._detach_restorer()
        self._connection.off("disconnected", self._bound_on_disconnected)

        if self._connection.is_connected and self._channel is not None:
            try:
                if self._consumer_tag:
                    await self._connection.cancel(self._channel, self._consumer_tag)
                await self._connection.close_channel(self._channel)
            except Exception as err:
                Logger.debug(f"{type(self).__name__}: error during close (may be expected): {err}")

        self._consumer_tag = ""
        self._channel = None
        self._is_initialized = False
        self._was_started = False
        self._bindings = []

    # -- queue configuration --------------------------------------------------

    def build_queue_arguments(self) -> Dict[str, Any]:
        """
        The ``arguments`` this listener's main queue is declared with. One
        method for both init() and _reinitialize(), because RabbitMQ fixes a
        queue's arguments at declare time and a disagreement between the two
        is a 406 on the first reconnection. Every key is added only when its
        option is set, so a listener that configures nothing declares ``{}``.
        """
        arguments: Dict[str, Any] = {}
        if self._message_ttl_ms is not None:
            arguments["x-message-ttl"] = self._message_ttl_ms
        if self._max_priority is not None:
            arguments["x-max-priority"] = self._max_priority
        return arguments

    def effective_prefetch(self) -> int:
        """
        Prefetch for late-ack consumers. Zero means *unlimited* to RabbitMQ,
        which with late ack lets the broker push an entire backlog into
        process memory — so an unset value falls back to Config.default_prefetch().
        """
        configured = self._max_concurrent
        if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
            return configured
        return Config.default_prefetch()

    def track_binding(self, routing_key: str) -> None:
        if routing_key not in self._bindings:
            self._bindings.append(routing_key)
