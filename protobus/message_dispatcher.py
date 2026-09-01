"""Message dispatcher for RPC communication."""

import asyncio
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

from aio_pika import ExchangeType, Message
from aio_pika.abc import AbstractChannel, AbstractExchange

from .callback_listener import CallbackListener
from .config import Config
from .connection import (
    IConnection,
    detach_listener,
    publish_confirmed,
    release_amqp_resources,
    schedule_amqp_release,
)
from .errors import (
    DisconnectedError,
    NotConnectedError,
    NotInitializedError,
    RpcTimeoutError,
    StreamBackpressureError,
    StreamSequenceError,
    StreamTimeoutError,
)
from .logger import Logger
from .priority import validate_message_priority


def _parse_final_header(headers: Dict[str, Any]) -> bool:
    """Read the x-protobus-final header tolerantly across AMQP client encodings."""
    v = headers.get(Config.HEADER_FINAL)
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8", errors="ignore")
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes")
    return bool(v)


def _parse_seq_header(headers: Dict[str, Any]) -> Optional[int]:
    """Read the x-protobus-seq header tolerantly across AMQP client encodings."""
    v = headers.get(Config.HEADER_SEQ)
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8", errors="ignore")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# Sentinel pushed into a stream's chunk queue to indicate "no more chunks."
_STREAM_END = object()


class _StreamFailure:
    """
    Sentinel carrying the reason a stream ended early.

    Distinct from _STREAM_END, which means "the producer finished". Pushing
    _STREAM_END on a disconnect made a truncated stream indistinguishable from
    a complete one: the caller's ``async for`` simply ended, and the missing
    tail looked like a short answer.
    """

    __slots__ = ("error",)

    def __init__(self, error: BaseException):
        self.error = error


class MessageDispatcher:
    """
    Dispatcher for RPC (Request-Response) message patterns.

    Manages pending callbacks and routes responses to waiting callers.
    """

    def __init__(self, connection: IConnection):
        """
        Initialize the message dispatcher.

        Args:
            connection: The connection to use
        """
        self._connection = connection
        self._channel: Optional[AbstractChannel] = None
        self._exchange: Optional[AbstractExchange] = None
        # Built once, here. Building it per reconnect leaked a channel, an
        # exclusive queue and a consumer each time — and because the discarded
        # listener stayed subscribed to the connection's reconnection events it
        # kept opening more channels, turning a linear leak into O(N^2).
        self._callback_listener: CallbackListener = CallbackListener(connection)
        self._pending_callbacks: Dict[str, asyncio.Future] = {}
        # correlation_id -> queue of streaming chunks. Replies for streaming
        # calls land here; the consuming async iterator drains them.
        self._pending_streams: Dict[str, asyncio.Queue] = {}
        # Next sequence number expected on each stream, so a gap is detected
        # rather than delivered as a shorter complete stream.
        self._stream_next_seq: Dict[str, int] = {}
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

    def _fail_stream(self, correlation_id: str, error: BaseException) -> None:
        """
        End a streaming call by raising, not by looking finished.

        The queue is left holding the failure so the consuming iterator raises
        on its next pull; the slot is dropped here so a late chunk for a dead
        stream is reported as unknown rather than accumulating.
        """
        queue = self._pending_streams.pop(correlation_id, None)
        self._stream_next_seq.pop(correlation_id, None)
        if queue is None:
            return
        try:
            queue.put_nowait(_StreamFailure(error))
        except Exception:  # pragma: no cover - unbounded queue
            pass

    async def init(self) -> None:
        """Initialize the message dispatcher."""
        async with self._setup_lock:
            if self._is_initialized:
                return
            await self._setup_channel()
            self._is_initialized = True

    async def _setup_channel(self) -> None:
        """
        Set up the dispatcher's own channel and, on first call, its callback
        listener.

        Also called on reconnect, so the previous channel is closed first. The
        CallbackListener is *not* rebuilt: it restores itself through its own
        inherited reconnection handler, including re-binding its (newly
        generated) callback queue to the callbacks exchange.
        """
        await self._teardown_channel()

        self._channel = await self._connection.open_channel()

        # Declare the main exchange
        self._exchange = await self._connection.ensure_exchange(
            self._channel,
            Config.bus_exchange_name(),
            ExchangeType.TOPIC,
        )

        # init()/start() are idempotent, so this is a no-op on reconnect.
        if not self._callback_listener.is_initialized:
            await self._callback_listener.init(self._on_result, "")
            await self._callback_listener.start()
        elif not self._callback_listener.is_ready:
            # Its own restore ran first and failed (or has not run yet).
            # is_initialized stays True either way, so without this the
            # dispatcher would log a successful reconnect while every reply
            # went to a queue that no longer exists.
            await self._callback_listener.restore()

        if not self._callback_listener.is_ready:
            raise NotConnectedError("Callback listener could not be restored")

        Logger.debug("MessageDispatcher initialized")

    async def _teardown_channel(self) -> None:
        """Best-effort release of the dispatcher's own channel."""
        channel = self._channel
        self._channel = None
        self._exchange = None
        await release_amqp_resources(channel)

    async def close(self) -> None:
        """Detach from the connection and release the dispatcher's resources."""
        if self._is_closed:
            return
        self._is_closed = True

        detach_listener(self._connection, "reconnected", self._bound_on_reconnected)
        detach_listener(
            self._connection, "disconnected", self._bound_on_disconnected
        )

        # Detaching removed the only thing that resolves in-flight calls, so
        # release them here rather than making every caller wait out its
        # timeout.
        self._fail_pending(DisconnectedError("Dispatcher closed"))

        await self._callback_listener.close()
        async with self._setup_lock:
            await self._teardown_channel()
        Logger.debug("MessageDispatcher closed")

    def _fail_pending(self, error: Exception) -> None:
        """Release every in-flight unary call and stream with `error`."""
        for _correlation_id, future in list(self._pending_callbacks.items()):
            if not future.done():
                future.set_exception(error)
        self._pending_callbacks.clear()

        # Fail any in-flight streams. This used to push _STREAM_END — the same
        # sentinel a *complete* stream ends with — so a caller whose stream was
        # cut in half saw its `async for` end normally and treated the prefix
        # as the whole answer. (The old comment here claimed the iterators
        # would raise StreamTimeoutError on their next idle window; they never
        # did, because the sentinel ends iteration before any idle window
        # arrives.)
        for correlation_id in list(self._pending_streams):
            self._fail_stream(correlation_id, error)
        self._pending_streams.clear()
        self._stream_next_seq.clear()

    async def _on_result(
        self,
        data: bytes,
        correlation_id: str,
        headers: Optional[Dict[str, Any]] = None,
    ) -> Optional[bytes]:
        """
        Handle incoming RPC responses (unary or streaming).

        Streaming replies arrive as multiple messages sharing one correlation_id.
        Each carries x-protobus-final in its headers; the last one has it set
        to true. We route by checking _pending_streams first, falling back to
        the unary _pending_callbacks map.
        """
        hdrs = headers or {}

        # Streaming reply path
        if correlation_id in self._pending_streams:
            queue = self._pending_streams[correlation_id]
            is_final = _parse_final_header(hdrs)

            # A gap in the sequence means a chunk was lost. Without this the
            # caller receives a shorter but apparently complete stream — the
            # producer's output silently truncated in the middle.
            seq = _parse_seq_header(hdrs)
            if seq is not None:
                expected = self._stream_next_seq.get(correlation_id, 0)
                if seq != expected:
                    self._fail_stream(
                        correlation_id,
                        StreamSequenceError(
                            f"Streaming chunk out of sequence: expected {expected}, "
                            f"got {seq}"
                        ),
                    )
                    return None
                self._stream_next_seq[correlation_id] = seq + 1

            # Non-empty payload → deliver. Empty body on a final-only terminal
            # is treated as "end of stream, no extra data."
            if data:
                # Bounded: a producer outrunning its consumer would otherwise
                # buffer without limit inside this dispatcher.
                if queue.qsize() >= Config.stream_max_buffered_chunks():
                    self._fail_stream(
                        correlation_id,
                        StreamBackpressureError(
                            f"Streaming reply buffer exceeded "
                            f"{Config.stream_max_buffered_chunks()} undelivered chunks"
                        ),
                    )
                    return None
                await queue.put(data)
            if is_final:
                await queue.put(_STREAM_END)
            return None

        # Unary reply path (existing behavior)
        if correlation_id in self._pending_callbacks:
            future = self._pending_callbacks.pop(correlation_id)
            if not future.done():
                future.set_result(data)
            return None

        Logger.warn(f"Received response for unknown correlation ID: {correlation_id}")
        return None

    async def publish(
        self,
        data: bytes,
        routing_key: str,
        rpc: bool = True,
        timeout_ms: Optional[int] = None,
        priority: Optional[int] = None,
    ) -> Optional[bytes]:
        """
        Publish a message and optionally wait for a response.

        Args:
            data: Message data to publish
            routing_key: Routing key for the message
            rpc: Whether to wait for a response
            timeout_ms: Timeout for RPC response in milliseconds
            priority: Optional AMQP message priority (0..255).

                Only meaningful when the destination queue was declared with
                ``x-max-priority``; publishing a priority to a queue without
                one is accepted and simply ignored by the broker (verified —
                this is what lets a new publisher talk to an old consumer).

                Priority reorders only messages STILL SITTING in the queue.
                Anything already prefetched by a consumer is not reordered, so
                with prefetch N across R replicas up to N*R messages can still
                be ahead of a high-priority one.

        Returns:
            Response data if rpc=True, None otherwise

        Raises:
            NotConnectedError: If not connected
            NotInitializedError: If not initialized
            asyncio.TimeoutError: If RPC times out
        """
        if not self._is_initialized:
            raise NotInitializedError("MessageDispatcher not initialized")

        if not self._connection.is_connected:
            raise NotConnectedError("Not connected to RabbitMQ")

        if not self._channel or not self._exchange:
            raise NotConnectedError("Channel or exchange not available")

        # Validate before anything is allocated or sent — see priority.py for
        # why aio-pika's own handling is not a safe backstop.
        priority = validate_message_priority(priority)

        correlation_id = str(uuid.uuid4())

        # Set up response future if RPC
        response_future: Optional[asyncio.Future] = None
        if rpc:
            if not self._callback_listener.is_ready:
                raise NotConnectedError("Callback listener not available")
            response_future = asyncio.get_event_loop().create_future()
            self._pending_callbacks[correlation_id] = response_future

        try:
            # Build message properties
            reply_to = self._callback_listener.callback_queue if rpc else None

            message = Message(
                body=data,
                correlation_id=correlation_id,
                reply_to=reply_to,
                priority=priority,
            )

            # Mandatory: a request nothing is bound to fails now rather than
            # after the caller's whole RPC timeout. This is the difference
            # between "that service is not running" and "that call was slow".
            await publish_confirmed(self._exchange, message, routing_key)
            Logger.debug(f"Published message to {routing_key}")

            # Wait for response if RPC.
            #
            # The deadline is the CALLER's (RPC_CALL_TIMEOUT_MS, 30s), not the
            # server's handler budget (MESSAGE_PROCESSING_TIMEOUT, 600s). Using
            # the latter meant a call to a service that was scaled to zero, or
            # simply not running, blocked its caller for ten minutes.
            if rpc and response_future:
                timeout = (timeout_ms or Config.rpc_call_timeout()) / 1000
                try:
                    return await asyncio.wait_for(response_future, timeout=timeout)
                except asyncio.TimeoutError:
                    # Clean up the pending callback
                    self._pending_callbacks.pop(correlation_id, None)
                    raise RpcTimeoutError(
                        f"No reply to {routing_key} within {timeout}s"
                    ) from None

            return None

        except Exception as e:
            # Clean up on error
            self._pending_callbacks.pop(correlation_id, None)
            raise

    async def publish_streaming(
        self,
        data: bytes,
        routing_key: str,
        stream_idle_timeout_ms: Optional[int] = None,
    ) -> AsyncIterator[bytes]:
        """
        Publish a request that expects a streaming reply.

        Returns an async iterator that yields each chunk's raw response bytes
        as they arrive on the callback queue. Iteration ends when a message
        with x-protobus-final=true arrives. Raises StreamTimeoutError if no
        chunk arrives within the idle timeout.

        Cleanup happens automatically when the iterator is exhausted, when the
        caller breaks out of the loop, or when an exception propagates.

        See ``docs/advanced/streaming.md`` for full semantics.
        """
        if not self._is_initialized:
            raise NotInitializedError("MessageDispatcher not initialized")
        if not self._connection.is_connected:
            raise NotConnectedError("Not connected to RabbitMQ")
        if not self._channel or not self._exchange:
            raise NotConnectedError("Channel or exchange not available")
        if not self._callback_listener.is_ready:
            raise NotConnectedError("Callback listener not available")

        correlation_id = str(uuid.uuid4())
        chunk_queue: asyncio.Queue = asyncio.Queue()
        self._pending_streams[correlation_id] = chunk_queue
        self._stream_next_seq[correlation_id] = 0

        try:
            reply_to = self._callback_listener.callback_queue
            message = Message(
                body=data,
                correlation_id=correlation_id,
                reply_to=reply_to,
            )
            await publish_confirmed(self._exchange, message, routing_key)
            Logger.debug(f"Published streaming request to {routing_key}")

            idle_timeout = (
                stream_idle_timeout_ms or Config.stream_idle_timeout()
            ) / 1000

            while True:
                try:
                    item = await asyncio.wait_for(
                        chunk_queue.get(), timeout=idle_timeout
                    )
                except asyncio.TimeoutError:
                    raise StreamTimeoutError(
                        f"No streaming chunk received within {idle_timeout}s"
                    )

                if item is _STREAM_END:
                    return

                # A stream that ended early raises. It must not be mistaken
                # for one that finished.
                if isinstance(item, _StreamFailure):
                    raise item.error

                yield item
        finally:
            self._pending_streams.pop(correlation_id, None)
            self._stream_next_seq.pop(correlation_id, None)

    async def _on_reconnected(self) -> None:
        """Handle reconnection event."""
        if not self._is_initialized or self._is_closed:
            return

        Logger.debug("MessageDispatcher reconnecting...")
        async with self._setup_lock:
            if self._is_closed:
                return
            try:
                await self._setup_channel()
                Logger.debug("MessageDispatcher reconnected")
            except Exception as e:
                Logger.error(f"Error reconnecting MessageDispatcher: {e}")

    def _on_disconnected(self) -> None:
        """Handle disconnection event."""
        Logger.debug("MessageDispatcher disconnected")

        self._fail_pending(
            DisconnectedError("Connection lost while waiting for response")
        )

        # Release rather than merely forget: an event delivered while the
        # connection is still up would otherwise strand a live channel.
        channel = self._channel
        self._channel = None
        self._exchange = None
        schedule_amqp_release(channel)
