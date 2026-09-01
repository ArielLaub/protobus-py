"""Base listener module for message queue consumption."""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aio_pika import ExchangeType
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractQueue

from .config import Config
from .connection import (
    Connection,
    IConnection,
    RetryOptions,
    detach_listener,
    release_amqp_resources,
    schedule_amqp_release,
)
from .errors import (
    AlreadyStartedError,
    ConnectionError,
    InvalidPriorityError,
    MissingExchangeError,
    NotConnectedError,
    NotInitializedError,
)
from .logger import Logger
from .priority import validate_max_priority

# Type alias for message handlers.
# Handlers may return bytes (unary reply), None (no reply), or an
# AsyncIterator[bytes] (streaming reply — see docs/advanced/streaming.md).
MessageHandler = Callable[[bytes, str, Dict[str, Any]], Awaitable[Any]]


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
        max_priority: Optional[int] = None,
    ):
        """
        Initialize the base listener.

        Args:
            connection: The connection to use
            late_ack: Whether to use late acknowledgment
            max_concurrent: Maximum concurrent messages (prefetch count)
            message_ttl_ms: Optional message TTL in milliseconds
            max_priority: Optional queue priority ceiling (``x-max-priority``).

                Defaults to None, which declares the queue with no
                ``x-max-priority`` argument at all — byte-identical to a
                listener from before priority support existed. This default is
                load-bearing: RabbitMQ answers a re-declare that adds
                ``x-max-priority`` to an existing queue with a 406
                PRECONDITION_FAILED, so a silent default would break every
                already-deployed service on upgrade.

                The 406 closes the channel it happened on. Each listener holds
                its own channel, so other listeners on the same connection do
                survive it (verified) — but the declare happens inside init(),
                so this listener never starts and MessageService.init()
                raises. The service fails to boot.

                Enabling it on a queue that already exists therefore needs a
                one-time drain, delete and recreate by an operator. See
                docs/advanced/message-priority.md.

                Recommended value: ``Config.RECOMMENDED_MAX_PRIORITY`` (2).
        """
        self._connection = connection
        self._late_ack = late_ack
        self._max_concurrent = max_concurrent
        self._message_ttl_ms = message_ttl_ms
        # Validated at construction, before anything is sent: an invalid value
        # would otherwise surface much later as a channel-killing 406 at
        # declare time, when there is nothing useful left to say about it.
        self._max_priority = validate_max_priority(max_priority)

        # Priority is refused outright when it could not possibly work.
        #
        # Ordering is only meaningful for messages still IN the queue. Without
        # a bounded prefetch the broker pushes the whole queue into this
        # consumer's buffer, and everything there is past reordering. Measured
        # against a real broker — 300 bulk messages arriving while the consumer
        # is already draining, then one control message:
        #
        #   max_concurrent=1, late_ack=True   -> control handled at 92 of 301
        #   max_concurrent=1, late_ack=False  -> control handled at 300 of 301
        #   max_concurrent=None               -> control handled at 300 of 301
        #
        # late_ack matters as much as the count because RabbitMQ ignores QoS
        # prefetch for auto-ack consumers, so `max_concurrent` alone buys
        # nothing. Both are required.
        #
        # This is refused rather than warned about because the failure is
        # otherwise invisible: the queue is correctly declared, an operator has
        # done the drain/delete/recreate migration to enable it, and the
        # feature simply does nothing with no signal anywhere.
        if self._max_priority is not None:
            if not max_concurrent:
                raise InvalidPriorityError(
                    "max_priority requires max_concurrent to be set. Without a "
                    "prefetch bound the broker pushes the whole queue to the "
                    "consumer, so priority cannot reorder anything and the "
                    "setting silently does nothing."
                )
            if not late_ack:
                raise InvalidPriorityError(
                    "max_priority requires late_ack=True. RabbitMQ ignores QoS "
                    "prefetch for auto-ack consumers, so max_concurrent alone "
                    "does not bound delivery and priority silently does "
                    "nothing. (MessageService sets late_ack for you whenever "
                    "max_concurrent is given.)"
                )

        self._channel: Optional[AbstractChannel] = None
        self._exchange: Optional[AbstractExchange] = None
        self._queue: Optional[AbstractQueue] = None
        self._queue_name: str = ""
        # The name asked for at init() time. Kept separately from _queue_name
        # because an anonymous queue's real name is broker-generated and must
        # NOT be re-declared verbatim after a reconnect (amq.* is reserved).
        self._configured_queue_name: str = ""
        self._is_anonymous: bool = True
        self._exchange_name: str = ""
        self._exchange_type: ExchangeType = ExchangeType.TOPIC

        self._bindings: List[str] = []
        self._handler: Optional[MessageHandler] = None
        self._consumer_tag: Optional[str] = None

        self._is_initialized = False
        self._was_started = False
        self._is_closed = False

        # Serialises channel setup. A flapping broker can deliver a second
        # 'reconnected' while the first re-setup is still awaiting a round-trip,
        # and _emit dispatches handlers with create_task, so the two would
        # otherwise interleave and each leave the other's channel and consumer
        # behind. _setup_channel/_teardown_channel assume the caller holds this.
        self._setup_lock = asyncio.Lock()

        # Set up connection event handlers. The bound refs are stored so close()
        # can unregister exactly these callbacks (TS parity: _boundOnReconnected).
        self._bound_on_reconnected = self._on_reconnected
        self._bound_on_disconnected = self._on_disconnected
        self._connection.on("reconnected", self._bound_on_reconnected)
        self._connection.on("disconnected", self._bound_on_disconnected)

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

    @property
    def is_ready(self) -> bool:
        """
        Whether the listener currently holds usable AMQP objects.

        Distinct from is_initialized, which stays True once set even if a
        later restore failed. Anything that depends on this listener's queue
        existing right now must check this, not is_initialized.
        """
        return self._channel is not None and self._queue is not None

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
        if self._is_closed:
            raise ConnectionError(
                "Listener has been closed and cannot be re-initialized"
            )

        if not self._exchange_name:
            raise MissingExchangeError("Exchange name not set")

        async with self._setup_lock:
            if self._is_initialized:
                return

            self._handler = handler
            self._queue_name = queue_name
            self._configured_queue_name = queue_name
            self._is_anonymous = not queue_name

            await self._setup_channel()
            self._is_initialized = True

    async def _setup_channel(self) -> None:
        """
        Set up the channel, exchange, and queue.

        Called both from init() and from _on_reconnected(), so it must release
        whatever it set up last time first — otherwise every reconnect leaks a
        channel (and its consumer) that nothing ever closes.
        """
        await self._teardown_channel()

        self._channel = await self._connection.open_channel()

        # Declare exchange
        self._exchange = await self._connection.ensure_exchange(
            self._channel,
            self._exchange_name,
            self._exchange_type,
        )

        # Prepare queue arguments.
        #
        # Every key here is opt-in. A listener that configured neither a TTL
        # nor a priority ceiling must reach ensure_queue with arguments=None,
        # exactly as it did before either feature existed — otherwise the
        # declare 406s against an already-deployed queue and this listener
        # never starts.
        arguments: Dict[str, Any] = {}
        if self._message_ttl_ms is not None:
            arguments["x-message-ttl"] = self._message_ttl_ms
        if self._max_priority is not None:
            arguments["x-max-priority"] = self._max_priority

        # Declare queue. Anonymous queues always ask for a fresh one: the
        # previous broker-generated name died with the previous connection.
        self._queue = await self._connection.ensure_queue(
            self._channel,
            "" if self._is_anonymous else self._configured_queue_name,
            arguments=arguments if arguments else None,
        )

        if self._queue:
            self._queue_name = self._queue.name

        # Direct-exchange listeners (callback queues) route by queue name, so
        # bind here rather than via subscribe() — the name changes on every
        # reconnect for anonymous queues and must be re-derived, not replayed.
        if self._exchange_type == ExchangeType.DIRECT and self._exchange:
            await self._connection.bind_queue(
                self._queue, self._exchange, self._queue_name
            )

        Logger.debug(f"Set up channel for queue: {self._queue_name}")

    async def _teardown_channel(self) -> None:
        """
        Best-effort release of the current consumer and channel.

        Everything here is tolerated failing: the usual caller is a reconnect,
        where the old channel died with the old connection.
        """
        consumer_tag, queue, channel = self._consumer_tag, self._queue, self._channel
        self._consumer_tag = None
        self._queue = None
        self._exchange = None
        self._channel = None

        await release_amqp_resources(channel, queue, consumer_tag)

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
        """Stop consuming, close the channel and detach from the connection."""
        if self._is_closed:
            return
        self._is_closed = True

        # Detach first: a reconnect racing with close() must not resurrect us.
        detach_listener(self._connection, "reconnected", self._bound_on_reconnected)
        detach_listener(
            self._connection, "disconnected", self._bound_on_disconnected
        )

        # An in-flight re-setup holds the lock; wait for it so we tear down the
        # channel it opened rather than leaving it behind.
        async with self._setup_lock:
            await self._teardown_channel()
        Logger.debug(f"Closed listener for queue: {self._queue_name}")

    async def _on_reconnected(self) -> None:
        """Handle reconnection event."""
        await self.restore()

    async def restore(self) -> None:
        """
        Rebuild channel, queue, bindings and consumer against the current
        connection. Safe to call concurrently and safe to call again after a
        previous attempt failed.
        """
        if not self._is_initialized or self._is_closed:
            # Never initialized (or already closed): nothing to restore, and
            # opening a channel here is a pure leak.
            return

        Logger.debug(f"Reconnected, reinitializing listener: {self._queue_name}")

        async with self._setup_lock:
            # Re-check: close() may have run while we waited for the lock.
            if self._is_closed:
                return

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
                if (
                    self._was_started
                    and self._handler
                    and self._channel
                    and self._queue
                ):
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
        """
        Handle disconnection event.

        State is cleared synchronously (callers must not see a dead channel),
        while the actual cancel/close is fire-and-forget: the connection is
        usually gone already, but when the event fires with the connection
        still up, dropping the reference would strand a live channel and a
        ghost consumer on the broker.
        """
        Logger.debug(f"Disconnected, clearing listener state: {self._queue_name}")

        consumer_tag, queue, channel = self._consumer_tag, self._queue, self._channel
        self._channel = None
        self._queue = None
        self._exchange = None
        self._consumer_tag = None

        schedule_amqp_release(channel, queue, consumer_tag)
