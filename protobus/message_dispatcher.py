"""Dispatcher for RPC: publishes requests and routes replies — unary and
streaming — back to the waiting caller."""

import asyncio
import uuid
import weakref
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Deque, Dict, Optional

from .callback_listener import CallbackListener
from .cancellation import AbortSignal
from .config import Config
from .connection import EXCHANGE_OPTIONS, IConnection, attach_restorer
from .errors import (
    ChannelClosedError,
    DisconnectedError,
    InvalidMessageIdError,
    NotConnectedError,
    PublishNackedError,
    RpcTimeoutError,
    StreamBackpressureError,
    StreamSequenceError,
    StreamTimeoutError,
    UnroutableError,
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

    __slots__ = ("chunks", "buffered_bytes", "last_seq", "waiter", "ended", "error", "owner")

    def __init__(self) -> None:
        self.chunks: Deque[bytes] = deque()
        self.buffered_bytes = 0
        self.last_seq: Optional[int] = None
        self.waiter: Optional["asyncio.Future[None]"] = None
        self.ended = False
        self.error: Optional[BaseException] = None
        # The StreamingReply consuming this entry, held WEAKLY: the entry is
        # what the dispatcher (and the reply's finalizer) hold, so a strong
        # reference here would keep every completed reply alive forever.
        self.owner: Optional["weakref.ReferenceType[StreamingReply]"] = None

    def reply(self) -> Optional["StreamingReply"]:
        return self.owner() if self.owner is not None else None

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
        await self._open()
        Logger.info("MessageDispatcher: successfully re-initialized after reconnection")

    async def _open(self) -> None:
        """
        Open the publishing channel and declare what it publishes to: the bus
        exchange for requests and the cancel exchange for stream cancellation
        notices. A caller that starts before any service has declared them
        would otherwise lose its channel to a NOT_FOUND on first publish.
        """
        channel = await self._connection.open_channel()
        await self._connection.declare_exchange(channel, Config.bus_exchange_name(), "topic", dict(EXCHANGE_OPTIONS))
        await self._connection.declare_exchange(channel, Config.cancel_exchange_name(), "fanout", dict(EXCHANGE_OPTIONS))
        self._channel = channel

    async def init(self) -> None:
        if self._is_initialized:
            return
        await self._open()
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
                self._fail_stream(stream, StreamSequenceError(
                    f"stream {correlation_id} lost at least one chunk: got seq={seq}, expected {expected}"
                ))
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
                self._fail_stream(stream, StreamBackpressureError(
                    f"stream {correlation_id} exceeded a buffer limit "
                    f"({len(stream.chunks) + 1} chunks / {would_be_bytes} bytes for this call, "
                    f"{would_be_total} bytes across all calls; limits are {max_chunks} chunks / "
                    f"{max_bytes} bytes / {max_total} bytes total) — the consumer is not keeping up with the producer"
                ))
                return
            stream.chunks.append(body)
            stream.buffered_bytes = would_be_bytes
            self._total_buffered_bytes = would_be_total
            reply = stream.reply()
            if reply is not None:
                reply._arm_idle()
        if is_final:
            stream.ended = True
        stream.wake()

    def _fail_stream(self, stream: _StreamEntry, error: BaseException) -> None:
        """
        Give up on a stream the dispatcher can no longer deliver correctly.

        The error stays on the entry so the consumer's next pull raises it;
        everything else — the buffer, the pending slot, and the producer on
        the other side — is released immediately, because the consumer may
        not pull again for a long time, or ever.
        """
        stream.error = error
        stream.ended = True
        self._drop_buffer(stream)
        reply = stream.reply()
        if reply is not None:
            reply._cancel(notify_only=True)
        stream.wake()

    @staticmethod
    def _forget_stream(dispatcher: "MessageDispatcher", correlation_id: str, stream: _StreamEntry) -> None:
        """Release a stream whose reply object was dropped without being
        closed or exhausted, so its buffer and slot do not outlive it."""
        if dispatcher._pending_streams.get(correlation_id) is stream:
            dispatcher._pending_streams.pop(correlation_id, None)
            dispatcher._drop_buffer(stream)

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

    async def _await_publishable_within(self, remaining: Callable[[], float], expired: Callable[[], BaseException]) -> None:
        """
        ``_await_publishable`` under a deadline. A connection that is usable
        right now is not sent through ``wait_for`` at all: that would cost a
        task hop on older Pythons and make an ordinary publish take extra
        loop iterations for nothing.
        """
        if self._connection.is_connected and not self._connection.is_reconnecting and getattr(self._connection, "is_ready", True):
            await self._await_publishable()
            return
        try:
            await asyncio.wait_for(self._await_publishable(), timeout=max(0.0, remaining()))
        except asyncio.TimeoutError:
            raise expired() from None

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
        rpc = rpc is not False
        if not rpc:
            # Fire-and-forget has no deadline: readiness is simply waited for.
            await self._await_publishable()

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

        def remaining() -> float:
            return deadline - loop.time()

        def timed_out(published: Optional[bool]) -> RpcTimeoutError:
            how_far = {False: "the request was never published", True: "the request was confirmed but no reply came"}.get(
                published, "the broker confirm was still outstanding, so the request may or may not have been delivered"  # type: ignore[arg-type]
            )
            return RpcTimeoutError(
                f"no reply for {routing_key} (correlationId {correlation_id}) within {limit}ms: {how_far}", published,
            )

        # Readiness counts against the deadline: a caller parked on a
        # reconnection is told at its own deadline, not the connection's.
        await self._await_publishable_within(remaining, lambda: timed_out(False))

        republished = False
        while True:
            if remaining() <= 0:
                # After a lost channel the first copy's fate is unknown.
                raise timed_out(None if republished else False)
            future: "asyncio.Future[bytes]" = loop.create_future()
            # Arm the reply slot BEFORE publishing: a fast service can reply
            # while the confirm is still in flight.
            self._callbacks[correlation_id] = _CallbackEntry(future, None)
            publish: "asyncio.Future[None]" = asyncio.ensure_future(self._connection.publish(
                self._channel, Config.bus_exchange_name(), routing_key, content, self._reply_to(properties),
            ))
            try:
                await asyncio.wait({publish, future}, timeout=max(0.0, remaining()), return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                # The caller gave up. Nothing owns the publish any more; it is
                # cancelled in turn, which _confirmed_publish handles.
                publish.cancel()
                self._release_callback(correlation_id, future)
                raise

            if future.done() and not future.cancelled() and future.exception() is None:
                # Replied, possibly before the confirm. The publish finishes
                # on its own; its outcome no longer matters to this call.
                self._detach_publish(publish, correlation_id)
                self._release_callback(correlation_id, future)
                return future.result()

            if future.done() and not publish.done():
                # The reply slot was failed by a disconnection while the
                # publish is still settling. Its outcome decides what this
                # is: a channel lost mid-send is republished once below, a
                # confirmed send makes it a genuine DisconnectedError.
                try:
                    await asyncio.wait({publish}, timeout=max(0.0, remaining()))
                except asyncio.CancelledError:
                    publish.cancel()
                    self._release_callback(correlation_id, future)
                    raise

            if publish.done():
                error = publish.exception()
                if error is None:
                    # Confirmed. Wait out the rest of the deadline for the reply
                    # (a slot already failed by a disconnection raises at once).
                    try:
                        replied, _ = await asyncio.wait({future}, timeout=max(0.0, remaining()))
                    except asyncio.CancelledError:
                        self._release_callback(correlation_id, future)
                        raise
                    self._release_callback(correlation_id, future)
                    if not replied:
                        raise timed_out(True)
                    return future.result()  # raises DisconnectedError etc. when the socket dropped
                self._release_callback(correlation_id, future)
                if isinstance(error, ChannelClosedError) and not republished and not (
                    self._connection.is_connected and not self._connection.is_reconnecting
                ):
                    # The socket died underneath the publish. The request is
                    # repeated once on the restored channel under the same
                    # message id — the outcome of the first copy is unknown,
                    # and a consumer that did receive it can recognise the
                    # second — if the deadline still allows it.
                    republished = True
                    Logger.debug(f"request {correlation_id} lost its channel to a disconnection; republishing once after recovery")
                    await self._await_publishable_within(remaining, lambda: timed_out(None))
                    continue
                # A publish failure wins over the deadline: "the request never
                # left" is the more specific answer.
                raise error

            # Deadline, with the confirm still outstanding: the broker may or
            # may not hold the request. It is not republished, and the
            # publish is left to settle on its own rather than cancelled
            # mid-send.
            self._detach_publish(publish, correlation_id)
            self._release_callback(correlation_id, future)
            raise timed_out(None)

    @staticmethod
    def _with_identity(properties: Dict[str, Any]) -> Dict[str, Any]:
        """Fix the message id before the first attempt, so a republish after a
        lost channel is recognisably the same message. Left absent from the
        caller-visible properties dict when it was not supplied."""
        if properties.get("message_id"):
            return properties
        return {**properties, "message_id": str(uuid.uuid4())}

    def _release_callback(self, correlation_id: str, future: "asyncio.Future[bytes]") -> None:
        """
        Free a reply slot on any exit, and make sure the local future can
        never report an unretrieved exception: a disconnection fails it
        whether or not anyone is still waiting on it.
        """
        entry = self._callbacks.get(correlation_id)
        if entry is not None and entry.future is future:
            self._callbacks.pop(correlation_id, None)
            if entry.timer is not None:
                entry.timer.cancel()
        if future.done():
            if not future.cancelled():
                future.exception()
        else:
            future.cancel()

    @staticmethod
    def _detach_publish(publish: "asyncio.Future[None]", correlation_id: str) -> None:
        """Let a publish nobody waits for any more run to completion, and
        consume its outcome so the loop never reports it as unretrieved."""
        if publish.done():
            if not publish.cancelled():
                publish.exception()
            return

        def consume(task: "asyncio.Future[None]") -> None:
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                Logger.debug(f"request {correlation_id}: publish settled after the call ended: {error!r}")

        publish.add_done_callback(consume)

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


def _publish_outcome_is_ambiguous(error: Optional[BaseException]) -> bool:
    """
    Whether a failed publish may nonetheless have reached the broker. Only a
    nack and an unroutable return are definite; a confirm timeout, a closed
    channel, and anything unexpected leave the request's fate unknown.
    """
    if error is None:
        return False
    return not isinstance(error, (PublishNackedError, UnroutableError))


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
        # Where the request is on its way to the broker. "unsent" can still
        # be withdrawn; "sending" is ambiguous; "sent" was confirmed.
        self._publish_state = "unsent"
        self._publish_error: Optional[BaseException] = None
        self._loop = asyncio.get_running_loop()

        dispatcher._pending_streams[self._id] = self._stream
        self._stream.owner = weakref.ref(self)
        # A reply dropped without being closed or exhausted must not keep its
        # buffer and slot alive in the dispatcher. The finalizer holds only
        # the dispatcher, the id and the entry — nothing that leads back to
        # this object — and is detached on every ordinary release.
        self._finalizer = weakref.finalize(self, MessageDispatcher._forget_stream, dispatcher, self._id, self._stream)

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
            if self._cancelled:
                # Closed while waiting for the connection: the request never
                # existed at the server, so there is nothing to send and
                # nothing to cancel.
                return
            self._publish_state = "sending"
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
            self._publish_state = "sent"
        except asyncio.CancelledError:
            raise
        except BaseException as err:
            self._publish_state = "failed"
            self._publish_error = err
            if self._released:
                # Already terminal — an idle deadline or a close got there
                # first. That outcome stands; a cancel notice, if one is due,
                # is sent by the done-callback _cancel registered.
                return
            self._stream.error = err
            self._stream.ended = True
            # Nothing is coming: release now rather than on the next pull,
            # which may never happen. The error stays for the consumer.
            if _publish_outcome_is_ambiguous(err):
                # The broker may hold the request and a producer may be
                # running for a caller that will never read: tell it.
                self._cancel(notify_only=True)
            else:
                self._release_call()
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
        detach = getattr(self._finalizer, "detach", None)
        if callable(detach):
            detach()
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
        task = self._publish_task
        if task is not None and not task.done():
            if self._publish_state == "unsent":
                # Still parked on readiness: withdraw it. Nothing reached the
                # server, so there is nothing to tell the server about.
                task.cancel()
                return
            # Mid-send. The notice must not overtake the request it cancels,
            # so it goes out once the send has settled.
            task.add_done_callback(lambda _t: self._notify())
            return
        self._notify()

    def _notify(self) -> None:
        """Tell the producer to stop, if a request may have reached it."""
        if self._publish_state == "unsent":
            return
        if self._publish_state == "failed" and not _publish_outcome_is_ambiguous(self._publish_error):
            # A definite publish failure — nacked, or returned unroutable:
            # the server never saw the request. Anything ambiguous (a confirm
            # timeout, a closed channel) may have reached a producer.
            return
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
        try:
            return await self._next()
        except asyncio.CancelledError:
            # The consuming task is going away, so nobody will read what the
            # producer sends next: stop it, as closing the stream would,
            # rather than letting it run to completion for nothing.
            self._cancel()
            raise

    async def _next(self) -> bytes:
        # Wait for the publish to settle once before consuming. Its outcome
        # is already on the entry (an error, or nothing); a publish that was
        # withdrawn by cancellation must not surface here as CancelledError.
        stream = self._stream
        if self._publish_task is not None:
            if stream.ended or stream.error is not None:
                # Already terminal — the idle deadline, a close or an abort
                # got here before the first pull, or a previous pull already
                # reported it. There is nothing to wait for; the send, if it
                # is still in flight, settles on its own.
                pass
            elif not self._publish_task.done():
                # Wait for the send to settle OR for the call to end first —
                # the idle deadline, a close, an abort. A send stalled on the
                # confirm must not hold the caller past its own deadline;
                # the send stays owned and settles in the background.
                waiter: "asyncio.Future[None]" = self._loop.create_future()
                stream.waiter = waiter
                try:
                    await asyncio.wait({self._publish_task, waiter}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    if stream.waiter is waiter:
                        stream.waiter = None
                    if waiter.done() and not waiter.cancelled():
                        waiter.exception()  # the error, if any, is on the entry
                    elif not waiter.done():
                        waiter.cancel()
            if self._publish_task.done():
                self._publish_task = None

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
