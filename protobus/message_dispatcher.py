"""Dispatcher for RPC: publishes requests and routes replies — unary and
streaming — back to the waiting caller."""

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Deque, Dict, Optional

from .callback_listener import CallbackListener
from .cancellation import AbortSignal
from .config import Config
from .connection import IConnection, attach_restorer
from .errors import (
    ChannelClosedError,
    DisconnectedError,
    InvalidMessageIdError,
    NotConnectedError,
    RpcTimeoutError,
    StreamBackpressureError,
    StreamSequenceError,
    StreamTimeoutError,
)
from .logger import Logger
from .priority import validate_message_priority


@dataclass
class CallOptions:
    """
    Per-call options for a unary RPC / fire-and-forget publish.

    ``priority``: AMQP message priority, 0-255. Only has an effect on a queue
    declared with ``max_priority``; a broker silently ignores it elsewhere.

    ``message_id``: the message's identity, as the consumer sees it in
    ``MessageHandlerContext.message_id``. Defaults to a fresh UUID. Set it to
    make a caller-driven republish recognisable: a PublishConfirmTimeoutError
    or ChannelClosedError leaves the outcome unknown, so calling again can
    produce two copies, and the same ``message_id`` on the second attempt is
    what lets an idempotent consumer see them as one. Derive it from the
    request (an order id), never from a clock or a counter. Rejected if
    blank, rather than quietly falling back to a UUID.
    """

    priority: Optional[int] = None
    message_id: Optional[str] = None


@dataclass
class StreamOptions:
    """
    Per-call streaming options.

    ``signal``: cancels the stream when aborted, from anywhere — a Stop
    button, an HTTP request's own cancellation, a timeout. Breaking out of
    the ``async for`` cancels too, once the iterator is closed (see
    ``publish_streaming``); a signal takes effect immediately.
    """

    signal: Optional[AbortSignal] = None


# AMQP carries message-id as a shortstr: one length byte, so 255 max.
MAX_MESSAGE_ID_BYTES = 255


def validate_message_id(message_id: Any) -> Optional[str]:
    """A caller-supplied message_id, or None to let the publish path mint one.
    Blank is refused rather than treated as absent."""
    if message_id is None:
        return None
    if not isinstance(message_id, str) or message_id.strip() == "":
        raise InvalidMessageIdError(
            f"message_id must be a non-empty string, got {message_id!r}. Leave it unset to have one generated."
        )
    size = len(message_id.encode("utf-8"))
    if size > MAX_MESSAGE_ID_BYTES:
        raise InvalidMessageIdError(
            f"message_id is {size} bytes; AMQP carries message-id as a shortstr, so it must be at "
            f"most {MAX_MESSAGE_ID_BYTES}. Hash a long key rather than concatenating it."
        )
    return message_id


def parse_seq_header(headers: Optional[Dict[str, Any]]) -> Optional[int]:
    """Read x-protobus-seq tolerantly. None when absent or unparseable, which
    disables validation rather than manufacturing a violation."""
    if not headers:
        return None
    v = headers.get(Config.HEADER_SEQ)
    if v is None or isinstance(v, bool):
        return None
    try:
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", errors="ignore")
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def parse_final_header(headers: Optional[Dict[str, Any]]) -> bool:
    """Read x-protobus-final tolerantly across AMQP client encodings."""
    if not headers:
        return False
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
        return v.lower() == "true" or v == "1"
    return bool(v)


class _CallbackEntry:
    __slots__ = ("future", "timer")

    def __init__(self, future: "asyncio.Future[bytes]", timer: Optional[asyncio.TimerHandle]) -> None:
        self.future = future
        self.timer = timer


class _StreamEntry:
    """A pending streaming RPC: replies arriving as multiple messages with
    the same correlation id, buffered until the consumer pulls them."""

    __slots__ = ("chunks", "buffered_bytes", "last_seq", "waiter", "ended", "error", "touch")

    def __init__(self) -> None:
        self.chunks: Deque[bytes] = deque()
        self.buffered_bytes = 0
        self.last_seq: Optional[int] = None
        self.waiter: Optional["asyncio.Future[None]"] = None
        self.ended = False
        self.error: Optional[BaseException] = None
        self.touch: Optional[Callable[[], None]] = None

    def wake(self, error: Optional[BaseException] = None) -> None:
        waiter, self.waiter = self.waiter, None
        if waiter is not None and not waiter.done():
            if error is not None:
                waiter.set_exception(error)
            else:
                waiter.set_result(None)


class MessageDispatcher:
    """Owns the publishing channel and the callback listener."""

    def __init__(self, connection: IConnection) -> None:
        self._connection = connection
        self._callbacks: Dict[str, _CallbackEntry] = {}
        # correlation id -> in-flight streaming reply state. Distinct from
        # _callbacks so a streaming reply cannot resolve the wrong future.
        self._pending_streams: Dict[str, _StreamEntry] = {}
        self._callback_listener = CallbackListener(connection)
        self._channel: Any = None
        # Bytes buffered across every pending stream; bounds the process.
        self._total_buffered_bytes = 0
        self._is_initialized = False
        self._cancel_tasks: set = set()

        self._bound_on_disconnected = self._on_disconnected
        self._connection.on("disconnected", self._bound_on_disconnected)
        self._detach_restorer = attach_restorer(connection, self._restore, "MessageDispatcher")

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def channel(self) -> Any:
        return self._channel

    @property
    def callback_listener(self) -> CallbackListener:
        return self._callback_listener

    @property
    def pending_callbacks(self) -> Dict[str, Any]:
        return self._callbacks

    @property
    def pending_streams(self) -> Dict[str, Any]:
        return self._pending_streams

    # -- connection lifecycle -------------------------------------------------

    def _on_disconnected(self, *_args: Any) -> None:
        """Reject every pending call: nothing will answer on the old socket."""
        Logger.debug("MessageDispatcher: connection lost, rejecting pending callbacks")
        self._channel = None
        error = DisconnectedError()
        for entry in list(self._callbacks.values()):
            if entry.timer is not None:
                entry.timer.cancel()
            if not entry.future.done():
                entry.future.set_exception(error)
        self._callbacks.clear()
        for stream in list(self._pending_streams.values()):
            stream.error = error
            stream.ended = True
            stream.wake(error)
        self._pending_streams.clear()
        self._total_buffered_bytes = 0

    async def _restore(self, _generation: int = 0) -> None:
        """Reopen the publishing channel. A failure propagates so the
        generation is retried rather than announced."""
        if not self._is_initialized:
            return
        Logger.info("MessageDispatcher: reconnected, re-initializing channel")
        self._channel = await self._connection.open_channel()
        Logger.info("MessageDispatcher: successfully re-initialized after reconnection")

    async def init(self) -> None:
        if self._is_initialized:
            return
        self._channel = await self._connection.open_channel()
        await self._callback_listener.init(self._on_result)
        await self._callback_listener.start()
        self._is_initialized = True

    async def close(self) -> None:
        self._connection.off("disconnected", self._bound_on_disconnected)
        self._detach_restorer()
        if self._callback_listener.is_initialized:
            await self._callback_listener.close()

    # -- replies --------------------------------------------------------------

    async def _on_result(self, content: bytes, correlation_id: str, headers: Optional[Dict[str, Any]] = None, *_args: Any) -> None:
        stream = self._pending_streams.get(correlation_id)
        if stream is not None:
            self._on_stream_chunk(stream, correlation_id, content, headers)
            return

        entry = self._callbacks.pop(correlation_id, None)
        if entry is not None:
            if entry.timer is not None:
                entry.timer.cancel()
            if not entry.future.done():
                entry.future.set_result(bytes(content))

    def _on_stream_chunk(self, stream: _StreamEntry, correlation_id: str, content: bytes, headers: Optional[Dict[str, Any]]) -> None:
        is_final = parse_final_header(headers)
        seq = parse_seq_header(headers)
        if seq is not None:
            expected = 0 if stream.last_seq is None else stream.last_seq + 1
            if seq < expected:
                # Already seen — a broker redelivery, not new data.
                Logger.debug(f"stream {correlation_id}: dropping duplicate chunk seq={seq} (expected {expected})")
                if is_final:
                    stream.ended = True
                stream.wake()
                return
            if seq > expected:
                stream.error = StreamSequenceError(
                    f"stream {correlation_id} lost at least one chunk: got seq={seq}, expected {expected}"
                )
                stream.ended = True
                self._drop_buffer(stream)
                stream.wake()
                return
            stream.last_seq = seq

        body = bytes(content) if content else b""
        if body:
            max_chunks = Config.stream_max_buffered_chunks()
            max_bytes = Config.stream_max_buffered_bytes()
            max_total = Config.stream_max_total_buffered_bytes()
            would_be_bytes = stream.buffered_bytes + len(body)
            would_be_total = self._total_buffered_bytes + len(body)
            if len(stream.chunks) + 1 > max_chunks or would_be_bytes > max_bytes or would_be_total > max_total:
                stream.error = StreamBackpressureError(
                    f"stream {correlation_id} exceeded a buffer limit "
                    f"({len(stream.chunks) + 1} chunks / {would_be_bytes} bytes for this call, "
                    f"{would_be_total} bytes across all calls; limits are {max_chunks} chunks / "
                    f"{max_bytes} bytes / {max_total} bytes total) — the consumer is not keeping up with the producer"
                )
                stream.ended = True
                self._drop_buffer(stream)
                stream.wake()
                return
            stream.chunks.append(body)
            stream.buffered_bytes = would_be_bytes
            self._total_buffered_bytes = would_be_total
            if stream.touch is not None:
                stream.touch()
        if is_final:
            stream.ended = True
        stream.wake()

    def _drop_buffer(self, stream: _StreamEntry) -> None:
        stream.chunks.clear()
        self._total_buffered_bytes = max(0, self._total_buffered_bytes - stream.buffered_bytes)
        stream.buffered_bytes = 0

    # -- publishing -----------------------------------------------------------

    async def _await_publishable(self) -> None:
        """
        Hold a publish until the connection can carry it. A reconnection in
        progress is waited through rather than failed on; anything else with
        no connection is a caller error.
        """
        if not self._connection.is_connected and not self._connection.is_reconnecting:
            raise NotConnectedError("not connected")
        when_ready = getattr(self._connection, "when_ready", None)
        if callable(when_ready):
            await when_ready()

    def _reply_to(self, properties: Dict[str, Any]) -> Dict[str, Any]:
        """
        The current callback queue, read at publish time rather than earlier.

        The callback queue is exclusive and auto-delete, so a reconnection
        replaces it; a request carrying the previous queue's name would be
        answered into nothing.
        """
        if properties.get("reply_to") is not None:
            return {**properties, "reply_to": self._callback_listener.callback_queue}
        return properties

    async def _publish_on_the_bus(self, routing_key: str, content: bytes, properties: Dict[str, Any]) -> None:
        """
        Publish to the bus exchange, riding out a socket that dies underneath
        the publish.

        A ChannelClosedError is an ambiguous outcome in general. When the
        cause is the connection going away — the socket died between the
        readiness check and the confirm — the publish is repeated once on the
        restored channel, under the SAME message id, so a consumer that did
        receive the first copy can recognise the second. The channel and the
        reply queue are re-read after readiness: a reconnection replaces both.
        """
        try:
            await self._connection.publish(self._channel, Config.bus_exchange_name(), routing_key, content, self._reply_to(properties))
        except ChannelClosedError:
            if self._connection.is_connected and not self._connection.is_reconnecting:
                raise
            Logger.debug(f"publish to {routing_key} lost its channel to a disconnection; republishing once after recovery")
            await self._await_publishable()
            await self._connection.publish(self._channel, Config.bus_exchange_name(), routing_key, content, self._reply_to(properties))

    async def publish(
        self,
        content: bytes,
        routing_key: str,
        rpc: bool = True,
        timeout_ms: Optional[int] = None,
        options: Optional[CallOptions] = None,
        priority: Optional[int] = None,
    ) -> Optional[bytes]:
        """
        Publish a request; with ``rpc`` wait for the reply.

        The deadline starts before the message is published, so it bounds
        the broker confirm as well as the reply. When more than one outcome
        is available, the publish result wins: a failed publish raises the
        broker's error rather than the expired deadline, because "the request
        never left" is the more specific answer.
        """
        options = options or CallOptions()
        if priority is not None and options.priority is None:
            options.priority = priority
        prio = validate_message_priority(options.priority)
        caller_message_id = validate_message_id(options.message_id)
        await self._await_publishable()
        rpc = rpc is not False

        correlation_id = str(uuid.uuid4())
        properties: Dict[str, Any] = {
            "content_type": "application/octet-stream",
            "correlation_id": correlation_id,
            "reply_to": self._callback_listener.callback_queue if rpc else None,
            "delivery_mode": 2,
            # An RPC request that routes nowhere is a definite error, worth
            # learning immediately rather than after a full RPC timeout.
            # Deliberately NOT set for events: no subscribers is normal.
            "mandatory": rpc,
        }
        if prio is not None:
            properties["priority"] = prio
        if caller_message_id is not None:
            properties["message_id"] = caller_message_id

        if not rpc:
            await self._publish_on_the_bus(routing_key, content, self._with_identity(properties))
            return None

        limit = timeout_ms if timeout_ms is not None else Config.rpc_call_timeout_ms()
        loop = asyncio.get_running_loop()
        properties = self._with_identity(properties)
        deadline = loop.time() + limit / 1000
        republished = False

        while True:
            remaining_ms = max(1, int((deadline - loop.time()) * 1000))
            future: "asyncio.Future[bytes]" = loop.create_future()

            # Arm the reply callback BEFORE publishing: a fast service can reply
            # while the confirm is still in flight.
            def on_timeout(future: "asyncio.Future[bytes]" = future) -> None:
                entry = self._callbacks.get(correlation_id)
                if entry is not None and entry.future is future:
                    self._callbacks.pop(correlation_id, None)
                    if not future.done():
                        future.set_exception(RpcTimeoutError(
                            f"no reply for {routing_key} (correlationId {correlation_id}) within {limit}ms"
                        ))

            timer = loop.call_later(remaining_ms / 1000, on_timeout)
            self._callbacks[correlation_id] = _CallbackEntry(future, timer)

            try:
                try:
                    await self._connection.publish(
                        self._channel, Config.bus_exchange_name(), routing_key, content, self._reply_to(properties),
                    )
                except ChannelClosedError:
                    # The socket died underneath the publish. When that is a
                    # disconnection (rather than a channel-level failure on a
                    # live connection), the request is repeated once on the
                    # restored channel under the same message id — see
                    # _publish_on_the_bus — with the reply slot re-armed, since
                    # the disconnect will have failed the one armed above.
                    if republished or (self._connection.is_connected and not self._connection.is_reconnecting):
                        raise
                    republished = True
                    self._release_callback(correlation_id, timer)
                    Logger.debug(f"request {correlation_id} lost its channel to a disconnection; republishing once after recovery")
                    await self._await_publishable()
                    continue
                except BaseException:
                    # The request never made it, so no reply is coming. Release
                    # the slot now and surface the publish failure — it wins over
                    # a deadline that may have expired meanwhile.
                    self._release_callback(correlation_id, timer)
                    raise
                return await future
            finally:
                # Cancellation-safe: whatever ended the wait, the slot is freed.
                self._release_callback(correlation_id, timer)

    @staticmethod
    def _with_identity(properties: Dict[str, Any]) -> Dict[str, Any]:
        """Fix the message id before the first attempt, so a republish after a
        lost channel is recognisably the same message. Left absent from the
        caller-visible properties dict when it was not supplied."""
        if properties.get("message_id"):
            return properties
        return {**properties, "message_id": str(uuid.uuid4())}

    def _release_callback(self, correlation_id: str, timer: asyncio.TimerHandle) -> None:
        timer.cancel()
        entry = self._callbacks.get(correlation_id)
        if entry is not None and entry.timer is timer:
            self._callbacks.pop(correlation_id, None)
            if not entry.future.done():
                entry.future.cancel()

    def publish_streaming(
        self,
        content: bytes,
        routing_key: str,
        idle_timeout_ms: Optional[int] = None,
        options: Optional[StreamOptions] = None,
    ) -> "StreamingReply":
        """
        Publish a request that expects a streaming reply. Returns an async
        iterator over raw reply bodies; iteration ends on x-protobus-final.
        Raises StreamTimeoutError if no chunk arrives within the idle timeout.

        Closing the iterator early — ``await stream.aclose()``, or ``async
        with stream:`` — releases the slot, stops buffering, and sends a
        best-effort cancellation notice to the server. A bare ``break`` out
        of ``async for`` does NOT close a Python async iterator by itself:
        wrap it in ``contextlib.aclosing`` or use the ``async with`` form.
        """
        if not self._connection.is_connected and not self._connection.is_reconnecting:
            raise NotConnectedError("not connected")
        options = options or StreamOptions()
        return StreamingReply(self, content, routing_key, idle_timeout_ms, options)


class StreamingReply:
    """The async iterator ``publish_streaming`` returns. See its docstring."""

    def __init__(
        self,
        dispatcher: MessageDispatcher,
        content: bytes,
        routing_key: str,
        idle_timeout_ms: Optional[int],
        options: StreamOptions,
    ) -> None:
        self._dispatcher = dispatcher
        self._id = str(uuid.uuid4())
        self._stream = _StreamEntry()
        self._timeout_ms = idle_timeout_ms if idle_timeout_ms is not None else Config.stream_idle_timeout_ms()
        self._idle_timer: Optional[asyncio.TimerHandle] = None
        self._cancelled = False
        self._released = False
        self._signal = options.signal
        self._publish_task: Optional["asyncio.Task[None]"] = None
        self._loop = asyncio.get_running_loop()

        dispatcher._pending_streams[self._id] = self._stream
        self._stream.touch = self._arm_idle

        aborted_before_start = self._signal is not None and self._signal.aborted
        if self._signal is not None and not aborted_before_start:
            self._signal.add_listener(self._on_abort)

        if aborted_before_start:
            # Aborted before it began: nothing to send and nothing to wait for.
            self._stream.ended = True
            dispatcher._pending_streams.pop(self._id, None)
        else:
            self._arm_idle()
            # Publish in the background; a failure surfaces on first iteration.
            # The channel is read after readiness, not before: a reconnection
            # replaces it, and capturing the old one would publish onto a
            # channel that is already gone.
            self._publish_task = self._loop.create_task(self._publish(content, routing_key))

    @property
    def correlation_id(self) -> str:
        return self._id

    async def _publish(self, content: bytes, routing_key: str) -> None:
        try:
            when_ready = getattr(self._dispatcher._connection, "when_ready", None)
            if callable(when_ready):
                await when_ready()
            await self._dispatcher._publish_on_the_bus(
                routing_key, content,
                {
                    "content_type": "application/octet-stream",
                    "correlation_id": self._id,
                    "reply_to": self._dispatcher._callback_listener.callback_queue,
                    "delivery_mode": 2,
                    "message_id": str(uuid.uuid4()),
                },
            )
        except asyncio.CancelledError:
            raise
        except BaseException as err:
            self._stream.error = err
            self._stream.ended = True
            self._stream.wake(err)

    # -- idle deadline --------------------------------------------------------

    def _clear_idle(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _arm_idle(self) -> None:
        """Idle deadline for the whole call, armed at creation rather than on
        the first pull, so a caller that never iterates still releases."""
        self._clear_idle()
        self._idle_timer = self._loop.call_later(self._timeout_ms / 1000, self._on_idle)

    def _on_idle(self) -> None:
        self._idle_timer = None
        if self._stream.ended:
            return
        self._stream.error = StreamTimeoutError(f"No streaming chunk received within {self._timeout_ms}ms")
        self._stream.ended = True
        # The producer is still generating for a caller that has stopped
        # listening, so tell it to stop.
        self._cancel(notify_only=True)
        self._stream.wake(self._stream.error)

    # -- release / cancel -----------------------------------------------------

    def _release_signal(self) -> None:
        if self._signal is not None:
            self._signal.remove_listener(self._on_abort)

    def _release_call(self) -> None:
        """Everything this call holds, released on any terminal outcome."""
        if self._released:
            return
        self._released = True
        self._clear_idle()
        self._release_signal()
        self._dispatcher._pending_streams.pop(self._id, None)
        self._dispatcher._drop_buffer(self._stream)

    def _on_abort(self) -> None:
        self._cancel()

    def _cancel(self, notify_only: bool = False) -> None:
        """
        Stop the producer and release everything this call holds. Best effort
        and delivered at most once: the notice is an ordinary message, and if
        it is lost the producer runs to completion — the same outcome as
        never cancelling.
        """
        if self._cancelled:
            return
        self._cancelled = True
        self._release_call()
        if not notify_only:
            self._stream.ended = True
            # Wake a consumer parked on the next chunk so it observes the end.
            self._stream.wake()

        Logger.debug(f"cancelling stream {self._id}")
        dispatcher = self._dispatcher
        channel = dispatcher._channel
        if channel is None:
            return

        async def notify() -> None:
            try:
                await dispatcher._connection.publish(
                    channel, Config.cancel_exchange_name(), "", b"",
                    {"correlation_id": self._id, "content_type": "application/octet-stream"},
                )
            except Exception as err:
                Logger.debug(f"failed to publish cancel for stream {self._id}: {err}")

        try:
            task = self._loop.create_task(notify())
        except RuntimeError:
            return
        dispatcher._cancel_tasks.add(task)
        task.add_done_callback(dispatcher._cancel_tasks.discard)

    # -- iteration ------------------------------------------------------------

    def __aiter__(self) -> "StreamingReply":
        return self

    async def __anext__(self) -> bytes:
        # Wait for the publish to settle once before consuming.
        if self._publish_task is not None:
            try:
                await asyncio.shield(self._publish_task)
            except asyncio.CancelledError:
                raise
            except BaseException:
                pass
            self._publish_task = None

        stream = self._stream
        while True:
            if stream.error is not None:
                error = stream.error
                self._release_call()
                raise error
            if stream.chunks:
                value = stream.chunks.popleft()
                stream.buffered_bytes -= len(value)
                self._dispatcher._total_buffered_bytes = max(0, self._dispatcher._total_buffered_bytes - len(value))
                self._arm_idle()
                return value
            if stream.ended:
                self._release_call()
                raise StopAsyncIteration
            waiter: "asyncio.Future[None]" = self._loop.create_future()
            stream.waiter = waiter
            try:
                await waiter
            except asyncio.CancelledError:
                if stream.waiter is waiter:
                    stream.waiter = None
                raise
            except BaseException:
                # The error is on the entry; the loop above raises it.
                pass

    async def aclose(self) -> None:
        """Stop the producer, not just our own buffering."""
        self._cancel()

    async def __aenter__(self) -> "StreamingReply":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()


AsyncStream = AsyncIterator[bytes]
