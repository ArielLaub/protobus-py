"""
The AMQP connection: sockets, channels, reconnection, publishing with broker
confirms, and the consume/settle ladder every listener runs on.

Built directly on ``aiormq`` (the protocol library under aio-pika), whose
channel API maps one-to-one onto the amqplib API the TypeScript port uses.
Reconnection is this module's own, generation-numbered and coordinated with
every component's restorer, rather than a client library's automatic one —
two reconnection mechanisms fighting over one socket is how a previous
version leaked channels and announced itself ready with no consumers.
"""

import asyncio
import inspect
import random
import time
import uuid
import weakref
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterable,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Set,
    Union,
    runtime_checkable,
)

import aiormq
from aiormq.abc import DeliveredMessage
from pamqp.commands import Basic

from .cancellation import AbortController, AbortSignal
from .config import Config
from .errors import (
    AlreadyConnectedError,
    ChannelClosedError,
    NotReadyError,
    PublishConfirmTimeoutError,
    PublishNackedError,
    ReconnectionError,
    TimeoutError,
    UnroutableError,
    safe_error_summary,
)
from .events import EventEmitter
from .logger import Logger, redact_url

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

Channel = aiormq.abc.AbstractChannel
"""An AMQP channel (aiormq). Opened with publisher confirms."""

MessageHandlerResult = Union[bytes, None, AsyncIterable[bytes]]
"""
What a MessageHandler may return:
  - ``bytes``                 -> a single unary reply is published
  - ``None``                  -> no reply (one-way event, or suppressed)
  - ``AsyncIterable[bytes]``  -> streaming reply; each chunk is published with
                                 x-protobus-final=false, the last with =true.
"""


@dataclass
class MessageHandlerContext:
    """
    Extra context handed to a message handler as its 4th argument.

    ``signal`` aborts when the processing timeout elapses or a streaming
    caller cancels. ``message_id`` is stable across every redelivery and
    every retry hop of the same logical message, which is what makes
    deduplication possible. ``redelivered`` is the broker's flag.
    """

    signal: AbortSignal
    routing_key: str
    message_id: Optional[str] = None
    redelivered: bool = False
    #: An encoded error reply for the processing-timeout path, set by a
    #: handler that knows how to encode one (MessageService does) so the
    #: caller hears about the timeout instead of waiting out its own deadline.
    timeout_reply: Optional[bytes] = None


MessageHandler = Callable[..., Awaitable[MessageHandlerResult]]
"""``async def handler(content: bytes, correlation_id: str, headers: dict,
context: MessageHandlerContext)``. A handler declaring only three positional
parameters is called without the context."""


@dataclass
class ConsumeOptions:
    consumer_tag: str = ""
    no_ack: bool = False
    exclusive: bool = False
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConsumeRetryOptions:
    """
    Retry ladder for a consumer.

    ``retry_exchange_name`` is the topic exchange the retry queue is bound to
    with ``#``. Publishing there (rather than straight to the queue) keeps the
    message's routing key set to the original ``REQUEST.<service>.<method>``,
    which is what makes the post-TTL DLX redelivery route back to the main
    queue. Without it the first hop works and the redelivery silently drops.
    """

    max_retries: int
    retry_queue_name: str
    dlq_name: str
    retry_exchange_name: Optional[str] = None
    is_handled_error: Optional[Callable[[Any], bool]] = None


@dataclass
class ReconnectionOptions:
    max_retries: int = 10  # 0 = infinite
    initial_delay_ms: int = 1000
    max_delay_ms: int = 30000
    backoff_multiplier: float = 2.0


DEFAULT_RECONNECTION_OPTIONS = ReconnectionOptions()

Restorer = Callable[[int], Awaitable[None]]
"""Restores one component's topology after the socket comes back. Receives
the generation it is restoring, so a long restore can tell it was superseded."""

#: Attribute a MessageService sets on an unhandled exception: the encoded
#: error reply the connection layer publishes on terminal settlement paths.
RESPONSE_BUFFER_ATTR = "protobus_response_buffer"


@runtime_checkable
class IConnection(Protocol):
    """
    What listeners and dispatchers need from a connection. Structural, so a
    test double satisfies it by shape. ``register_restorer``, ``when_ready``,
    ``is_ready`` and ``cancel_stream`` are optional — a double without them
    gets the uncoordinated fallbacks.
    """

    @property
    def is_connected(self) -> bool: ...

    @property
    def is_reconnecting(self) -> bool: ...

    def on(self, event: str, callback: Callable[..., Any]) -> None: ...

    def off(self, event: str, callback: Callable[..., Any]) -> None: ...

    async def open_channel(self) -> Any: ...

    async def close_channel(self, channel: Any) -> Any: ...

    async def declare_exchange(self, channel: Any, exchange: str, exchange_type: str, options: Dict[str, Any]) -> Any: ...

    async def declare_queue(self, channel: Any, queue_name: str, options: Dict[str, Any]) -> str: ...

    async def bind_queue(self, channel: Any, queue: str, exchange: str, routing_key: str, args: Any) -> Any: ...

    async def consume(
        self,
        channel: Any,
        queue_name: str,
        message_handler: MessageHandler,
        options: ConsumeOptions,
        late_ack: bool,
        retry_options: Optional[ConsumeRetryOptions] = None,
        processing_timeout_ms: Optional[int] = None,
    ) -> Any: ...

    async def cancel(self, channel: Any, consumer_tag: str) -> Any: ...

    async def publish(self, channel: Any, exchange_name: str, routing_key: str, content: bytes, properties: Dict[str, Any]) -> Any: ...


def attach_restorer(connection: Any, restore: Callable[..., Awaitable[None]], describe: str) -> Callable[[], None]:
    """
    Wire a component's restoration to a connection, returning a detach function.

    Prefers the coordinated path, where the connection holds back its
    'reconnected' announcement until the restore has finished and treats a
    failure as a failed reconnection attempt. Falls back to restoring on the
    event for a connection that predates ``register_restorer`` — uncoordinated,
    so a failure there can only be logged.
    """
    register = getattr(connection, "register_restorer", None)
    if callable(register):
        return register(restore)

    async def on_reconnected(*_args: Any) -> None:
        try:
            await _call_restorer(restore, 0)
        except Exception as err:
            Logger.error(f"{describe}: failed to re-initialize after reconnection: {err}")

    connection.on("reconnected", on_reconnected)

    def detach() -> None:
        connection.off("reconnected", on_reconnected)

    return detach


async def _call_restorer(restore: Callable[..., Awaitable[None]], generation: int) -> None:
    """Call a restorer with the generation if it accepts one argument."""
    try:
        params = inspect.signature(restore).parameters
        accepts = any(
            p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
            for p in params.values()
        )
    except (TypeError, ValueError):
        accepts = False
    result = restore(generation) if accepts else restore()
    if inspect.isawaitable(result):
        await result


def apply_heartbeat(url: str) -> str:
    """
    Put the configured heartbeat on a broker URL.

    A heartbeat already in the URL is the caller being explicit and is left
    alone, ``heartbeat=0`` included — that is how they are disabled.
    Everything else about the URL is preserved byte for byte: the vhost is
    routinely percent-encoded (``/%2f``) and re-encoding it would connect to
    the wrong one.
    """
    if not isinstance(url, str) or "://" not in url:
        return url
    base, sep, query = url.partition("?")
    if sep and any(param.split("=", 1)[0].strip() == "heartbeat" for param in query.split("&") if param):
        return url
    seconds = Config.heartbeat_seconds()
    if sep and query:
        return f"{base}?{query}&heartbeat={seconds}"
    return f"{base}?heartbeat={seconds}"


# ---------------------------------------------------------------------------
# AMQP property handling
# ---------------------------------------------------------------------------

# AMQP properties a republish copies from the original delivery. Anything
# not named here is dropped by the retry and DLQ hops, which build their
# properties by hand. `delivery_mode` is re-expressed as persistent at every
# site; `expiration` would race the retry queue's TTL or delete DLQ evidence;
# `user_id` is validated by the broker against the *publishing* connection,
# which need not be the original publisher's.
CARRIED_PROPERTIES = ("content_type", "content_encoding", "priority", "timestamp", "message_type", "app_id")

# Names a caller may use in a properties dict, mapped onto pamqp's.
_PROPERTY_ALIASES = {
    "contentType": "content_type",
    "contentEncoding": "content_encoding",
    "correlationId": "correlation_id",
    "replyTo": "reply_to",
    "messageId": "message_id",
    "deliveryMode": "delivery_mode",
    "appId": "app_id",
    "userId": "user_id",
    "type": "message_type",
    "clusterId": "cluster_id",
}

_PROPERTY_FIELDS = (
    "content_type", "content_encoding", "headers", "delivery_mode", "priority", "correlation_id",
    "reply_to", "expiration", "message_id", "timestamp", "message_type", "user_id", "app_id", "cluster_id",
)


def carried_properties(properties: Basic.Properties) -> Dict[str, Any]:
    """The subset of a delivery's properties a republish carries. Absent stays absent."""
    carried: Dict[str, Any] = {}
    for key in CARRIED_PROPERTIES:
        value = getattr(properties, key, None)
        if value is not None:
            carried[key] = value
    return carried


def build_properties(properties: Optional[Dict[str, Any]]) -> Basic.Properties:
    """Turn a properties dict into pamqp ``Basic.Properties``."""
    props: Dict[str, Any] = {}
    for key, value in (properties or {}).items():
        key = _PROPERTY_ALIASES.get(key, key)
        if key == "persistent":
            if value:
                props["delivery_mode"] = 2
            continue
        if key == "mandatory":
            continue
        if key not in _PROPERTY_FIELDS or value is None:
            continue
        props[key] = value
    if "expiration" in props and not isinstance(props["expiration"], str):
        # AMQP carries expiration as a string of milliseconds.
        props["expiration"] = str(int(props["expiration"]))
    if props.get("headers") is not None:
        props["headers"] = sanitize_headers(props["headers"])
    return Basic.Properties(**props)


def sanitize_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce header values into what the AMQP field table can carry."""
    clean: Dict[str, Any] = {}
    for key, value in headers.items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float, str, bytes, list, dict)):
            clean[str(key)] = value
        else:
            clean[str(key)] = str(value)
    return clean


def header_int(headers: Dict[str, Any], name: str, default: int = 0) -> int:
    value = headers.get(name)
    if value is None or isinstance(value, bool):
        return default
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="ignore")
        return int(value)
    except (TypeError, ValueError):
        return default


def header_str(headers: Dict[str, Any], name: str) -> Optional[str]:
    value = headers.get(name)
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def now_ms() -> int:
    return int(time.time() * 1000)


def _handler_arity(handler: Callable[..., Any]) -> int:
    """How many positional arguments a handler accepts; 4 for *args."""
    try:
        params = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        return 4
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return 4
    return len([p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)])


def _is_async_iterable(value: Any) -> bool:
    return value is not None and not isinstance(value, (bytes, bytearray)) and hasattr(value, "__aiter__")


# ---------------------------------------------------------------------------
# Per-channel publish bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _ChannelPublishState:
    in_flight: int = 0
    waiters: List["asyncio.Future[None]"] = field(default_factory=list)
    # Futures of publishes awaiting a confirm, failed when the channel closes.
    pending: Set["asyncio.Future[Any]"] = field(default_factory=set)


@dataclass(eq=False)
class _Delivery:
    """This attempt's cancellation state, so cancel_stream() can reach it."""

    controller: AbortController
    cancelled: bool = False


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class Connection(EventEmitter):
    """
    RabbitMQ connection with generation-numbered reconnection.

    Events: 'reconnecting' ({attempt, delay}), 'reconnected', 'disconnected',
    'error' (exception).
    """

    def __init__(self, options: Optional[ReconnectionOptions] = None) -> None:
        super().__init__()
        self._handle: Optional[aiormq.abc.AbstractConnection] = None
        self._url: str = ""
        self._reconnection_options: ReconnectionOptions = options or DEFAULT_RECONNECTION_OPTIONS
        self._reconnect_attempts = 0
        self._reconnect_timer: Optional[asyncio.TimerHandle] = None
        self._reconnect_task: Optional["asyncio.Task[None]"] = None
        self._manual_disconnect = False
        # In-flight connect shared by concurrent callers; see _connect().
        self._connect_task: Optional["asyncio.Task[Any]"] = None
        # Bumped by every teardown. A connect completing with a stale
        # generation is obsolete and closes itself rather than installing.
        self._generation = 0

        self._is_connected = False
        self._is_reconnecting = False

        self._restorers: List[Callable[..., Awaitable[None]]] = []
        self._is_ready = False
        self._ready_waiters: List["asyncio.Future[None]"] = []

        self._active_deliveries: Dict[str, Set[_Delivery]] = {}
        self._in_flight_deliveries = 0
        self._running_handlers = 0
        self._drain_waiters: List["asyncio.Future[None]"] = []
        self._handler_tasks: Set["asyncio.Task[Any]"] = set()

        self._publish_state: "weakref.WeakKeyDictionary[Any, _ChannelPublishState]" = weakref.WeakKeyDictionary()
        self._publish_state_fallback: Dict[int, _ChannelPublishState] = {}
        self._closing_callback: Optional[Callable[[Any], None]] = None

    # -- state ----------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_reconnecting(self) -> bool:
        return self._is_reconnecting

    @property
    def is_ready(self) -> bool:
        """True when the socket is up AND every restorer has finished."""
        return self._is_ready

    @property
    def handle(self) -> Optional[aiormq.abc.AbstractConnection]:
        """The underlying aiormq connection, for tests and diagnostics."""
        return self._handle

    @property
    def generation(self) -> int:
        return self._generation

    # -- restorers & readiness ------------------------------------------------

    def register_restorer(self, restore: Callable[..., Awaitable[None]]) -> Callable[[], None]:
        """
        Register topology to restore on reconnection.

        Restorers run in registration order and the connection reports itself
        reconnected only once they have all resolved. One that raises makes
        the whole generation unusable, which is treated as a failed
        reconnection attempt.

        Returns a function that unregisters the restorer again.
        """
        self._restorers.append(restore)

        def detach() -> None:
            try:
                self._restorers.remove(restore)
            except ValueError:
                pass

        return detach

    async def when_ready(self, timeout_ms: Optional[int] = None) -> None:
        """
        Wait until the connection is carrying traffic again.

        Resolves at once when already ready. Raises NotReadyError if the
        connection is closed or abandons reconnection while waiting, or if
        the wait exceeds ``timeout_ms``.
        """
        if self._is_ready:
            return
        if self._manual_disconnect:
            raise NotReadyError("the connection has been closed")
        limit = Config.connection_ready_timeout_ms() if timeout_ms is None else timeout_ms

        waiter: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
        self._ready_waiters.append(waiter)
        try:
            await asyncio.wait_for(asyncio.shield(waiter), limit / 1000)
        except asyncio.TimeoutError:
            raise NotReadyError(f"the connection did not become ready within {limit}ms") from None
        finally:
            try:
                self._ready_waiters.remove(waiter)
            except ValueError:
                pass
            if not waiter.done():
                waiter.cancel()

    def _mark_ready(self) -> None:
        self._is_ready = True
        waiting, self._ready_waiters = self._ready_waiters, []
        for w in waiting:
            if not w.done():
                w.set_result(None)

    def _mark_not_ready(self) -> None:
        # Waiters stay parked: a reconnection is what they are waiting through.
        self._is_ready = False

    def _abandon_ready(self, err: Exception) -> None:
        self._is_ready = False
        waiting, self._ready_waiters = self._ready_waiters, []
        for w in waiting:
            if not w.done():
                w.set_exception(err)

    async def _run_restorers(self, generation: int) -> None:
        """Put every component's topology back, in registration order. Sequential:
        one component's restore declares the exchange another binds to."""
        for restore in list(self._restorers):
            if generation != self._generation:
                raise ReconnectionError("connection was torn down while restoring")
            await _call_restorer(restore, generation)
        # A socket that died during the last restorer has its close callback
        # queued but not yet run; let it run before deciding this generation
        # is healthy, and look at the socket itself as well.
        await asyncio.sleep(0)
        if generation != self._generation or self._handle is None or self._handle.closing.done():
            raise ReconnectionError("connection was torn down while restoring")

    async def _discard_generation(self) -> None:
        """Discard a generation that connected but could not be restored."""
        self._generation += 1
        self._mark_not_ready()
        self._is_connected = False
        handle, self._handle = self._handle, None
        if handle is None:
            return
        self._detach_close_callback(handle)
        try:
            await handle.close()
        except Exception as err:
            Logger.debug(f"failed closing an unrestorable connection: {err}")

    # -- stream cancellation --------------------------------------------------

    def cancel_stream(self, correlation_id: str) -> bool:
        """
        Stop producing a streaming reply the caller has abandoned.

        Aborts the handler's signal and stops publishing anything the
        generator yields from here on. Cooperative: a generator that ignores
        its signal keeps running, but its output is no longer sent anywhere.
        Returns True if a matching in-flight delivery was found.
        """
        entries = self._active_deliveries.get(correlation_id)
        if not entries:
            return False
        # The same message can legitimately be in flight more than once — a
        # redelivery overlapping its predecessor — and all of them stop.
        for entry in list(entries):
            entry.cancelled = True
            entry.controller.abort("cancelled by the caller")
        Logger.debug(f"stream {correlation_id} cancelled by the caller")
        return True

    # -- in-flight accounting -------------------------------------------------

    @property
    def in_flight_deliveries(self) -> int:
        """How many messages are currently being handled."""
        return max(self._in_flight_deliveries, self._running_handlers)

    async def drain_in_flight(self, timeout_ms: int) -> bool:
        """
        Wait for in-flight handlers to finish, up to ``timeout_ms``. Returns
        True if everything drained, False if the deadline passed with work
        still running — information for the caller, not an error.
        """
        if self.in_flight_deliveries == 0:
            return True
        waiter: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
        self._drain_waiters.append(waiter)
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout_ms / 1000)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            try:
                self._drain_waiters.remove(waiter)
            except ValueError:
                pass

    def _delivery_started(self) -> None:
        self._in_flight_deliveries += 1

    def _delivery_finished(self) -> None:
        self._in_flight_deliveries = max(0, self._in_flight_deliveries - 1)
        self._maybe_drained()

    def _handler_started(self) -> None:
        self._running_handlers += 1

    def _handler_finished(self) -> None:
        self._running_handlers = max(0, self._running_handlers - 1)
        self._maybe_drained()

    def _maybe_drained(self) -> None:
        if self.in_flight_deliveries > 0:
            return
        waiters, self._drain_waiters = self._drain_waiters, []
        for w in waiters:
            if not w.done():
                w.set_result(None)

    # -- connect / disconnect -------------------------------------------------

    async def connect(self, url: str, reconnection_options: Optional[ReconnectionOptions] = None) -> Any:
        if self.is_connected:
            raise AlreadyConnectedError("already connected")
        self._url = url
        if reconnection_options is not None:
            self._reconnection_options = reconnection_options
        self._manual_disconnect = False

        handle = await self._connect()
        # Nothing to restore on a first connect — components initialise
        # themselves against it — so the socket coming up is readiness.
        self._is_reconnecting = False
        self._mark_ready()
        return handle

    async def _connect(self) -> Any:
        """Single-flight connect: concurrent callers share one attempt."""
        if self._connect_task is not None and not self._connect_task.done():
            return await asyncio.shield(self._connect_task)
        self._connect_task = asyncio.get_running_loop().create_task(self._do_connect())
        try:
            return await asyncio.shield(self._connect_task)
        finally:
            if self._connect_task is not None and self._connect_task.done():
                self._connect_task = None

    async def _do_connect(self) -> Any:
        Logger.info(f"connecting to bus - {redact_url(self._url)}")
        generation = self._generation
        try:
            handle = await aiormq.connect(apply_heartbeat(self._url))
        except Exception as err:
            Logger.error(f"failed to connect: {err}")
            self._is_connected = False
            raise

        if generation != self._generation or self._manual_disconnect:
            # Torn down while this attempt was in flight. Clearing the
            # reconnect timer cannot stop an attempt that has already fired.
            Logger.info("discarding a connection that completed after disconnect")
            try:
                await handle.close()
            except Exception as err:
                Logger.debug(f"failed closing a superseded connection: {err}")
            raise ReconnectionError("connection was torn down while connecting")

        self._handle = handle
        self._is_connected = True
        self._reconnect_attempts = 0
        # _is_reconnecting is deliberately NOT cleared here: on the reconnect
        # path a socket is only half the job, restoration still has to run.
        # Whoever set the flag clears it — connect(), or the reconnect task.
        self._attach_close_callback(handle)
        Logger.info("connected to message bus")
        return handle

    def _attach_close_callback(self, handle: Any) -> None:
        def on_closed(future: "asyncio.Future[Any]") -> None:
            self._on_handle_closed(handle, future)

        setattr(handle, "_protobus_on_closed", on_closed)
        handle.closing.add_done_callback(on_closed)

    def _detach_close_callback(self, handle: Any) -> None:
        cb = getattr(handle, "_protobus_on_closed", None)
        if cb is not None:
            try:
                handle.closing.remove_done_callback(cb)
            except Exception:
                pass

    def _on_handle_closed(self, handle: Any, future: "asyncio.Future[Any]") -> None:
        if handle is not self._handle:
            return
        if self._manual_disconnect:
            Logger.info("connection closed (manual disconnect)")
            return
        exc: Any = None
        try:
            exc = future.exception() if not future.cancelled() else None
        except Exception:
            exc = None
        if isinstance(exc, asyncio.CancelledError) or future.cancelled():
            # The event loop is being torn down (an `asyncio.run()` ending
            # with the connection still open): not a broker failure, and
            # nothing to reconnect to.
            Logger.debug("connection closed by event loop shutdown")
            self._is_connected = False
            self._mark_not_ready()
            self._generation += 1
            return
        if exc is not None:
            Logger.error(f"connection error: {exc}")
            self.emit("error", exc)
        Logger.warn("connection closed unexpectedly")
        self._is_connected = False
        self._mark_not_ready()
        # Retire this generation: a restoration in flight against it is now
        # working on a dead socket and its next generation check aborts it.
        self._generation += 1
        self.emit("disconnected")
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._manual_disconnect or self._is_reconnecting:
            return
        opts = self._reconnection_options
        if opts.max_retries > 0 and self._reconnect_attempts >= opts.max_retries:
            error = ReconnectionError(f"max reconnection attempts ({opts.max_retries}) exceeded")
            Logger.error(str(error))
            self._abandon_ready(NotReadyError(str(error)))
            self.emit("error", error)
            return

        self._is_reconnecting = True
        self._reconnect_attempts += 1

        base_delay = min(opts.initial_delay_ms * (opts.backoff_multiplier ** (self._reconnect_attempts - 1)), opts.max_delay_ms)
        delay = int(base_delay + random.random() * 0.3 * base_delay)
        Logger.info(f"scheduling reconnection attempt {self._reconnect_attempts} in {delay}ms")
        self.emit("reconnecting", {"attempt": self._reconnect_attempts, "delay": delay})

        loop = asyncio.get_running_loop()

        def fire() -> None:
            self._reconnect_timer = None
            self._reconnect_task = loop.create_task(self._reconnect_attempt())

        self._reconnect_timer = loop.call_later(delay / 1000, fire)

    async def _reconnect_attempt(self) -> None:
        # Read the counter before connecting: a successful connect resets it.
        attempt = self._reconnect_attempts
        try:
            await self._connect()
            # Restoration is part of reconnecting, not something that happens
            # afterwards: until every listener has its channel, queue and
            # consumer back, the socket is up but the application cannot use it.
            await self._run_restorers(self._generation)
            self._is_reconnecting = False
            self._mark_ready()
            Logger.info(f"reconnection successful after {attempt} attempts")
            self.emit("reconnected")
        except asyncio.CancelledError:
            self._is_reconnecting = False
            raise
        except Exception as err:
            if self._manual_disconnect:
                self._is_reconnecting = False
                return
            Logger.error(f"reconnection attempt {attempt} failed: {err}")
            # A generation that connected but could not be restored is worse
            # than no connection: it looks healthy and serves nothing.
            await self._discard_generation()
            self._reconnect_attempts = attempt
            self._is_reconnecting = False
            self._schedule_reconnect()

    async def disconnect(self) -> None:
        self._manual_disconnect = True
        # Invalidate any connect already past its timer.
        self._generation += 1
        if self._reconnect_timer is not None:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
        task, self._reconnect_task = self._reconnect_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._is_reconnecting = False
        self._abandon_ready(NotReadyError("the connection has been closed"))

        handle, self._handle = self._handle, None
        if handle is not None:
            self._detach_close_callback(handle)
            try:
                await handle.close()
            except Exception as err:
                Logger.debug(f"error closing connection: {err}")
        self._is_connected = False

    # 1.x name.
    close = disconnect

    # -- channels -------------------------------------------------------------

    async def open_channel(self) -> Channel:
        """Open a CONFIRM channel. A plain channel gives the publisher no way
        to learn whether RabbitMQ accepted a message."""
        if self._handle is None:
            raise NotReadyError("not connected")
        return await self._handle.channel(publisher_confirms=True, on_return_raises=False)

    async def close_channel(self, channel: Channel) -> None:
        await channel.close()

    async def set_prefetch(self, channel: Channel, count: int) -> None:
        await channel.basic_qos(prefetch_count=count)

    async def declare_exchange(self, channel: Channel, exchange: str, exchange_type: str, options: Optional[Dict[str, Any]] = None) -> Any:
        options = options or {}
        return await channel.exchange_declare(
            exchange=exchange,
            exchange_type=exchange_type,
            durable=options.get("durable", True),
            auto_delete=options.get("auto_delete", options.get("autoDelete", False)),
            internal=options.get("internal", False),
            arguments=options.get("arguments") or None,
        )

    async def declare_queue(self, channel: Channel, queue_name: str, options: Optional[Dict[str, Any]] = None) -> str:
        options = options or {}
        result = await channel.queue_declare(
            queue=queue_name or "",
            durable=options.get("durable", False),
            exclusive=options.get("exclusive", False),
            auto_delete=options.get("auto_delete", options.get("autoDelete", False)),
            arguments=options.get("arguments") or None,
        )
        return result.queue

    async def bind_queue(self, channel: Channel, queue: str, exchange: str, routing_key: str, args: Optional[Dict[str, Any]] = None) -> Any:
        return await channel.queue_bind(queue=queue, exchange=exchange, routing_key=routing_key, arguments=args or None)

    async def unbind_queue(self, channel: Channel, queue: str, exchange: str, routing_key: str, args: Optional[Dict[str, Any]] = None) -> Any:
        return await channel.queue_unbind(queue=queue, exchange=exchange, routing_key=routing_key, arguments=args or None)

    async def delete_queue(self, channel: Channel, queue_name: str) -> Any:
        return await channel.queue_delete(queue=queue_name)

    async def purge_queue(self, channel: Channel, queue_name: str) -> Any:
        return await channel.queue_purge(queue=queue_name)

    async def ack(self, channel: Channel, message: DeliveredMessage, up_to: bool = False) -> Any:
        return await channel.basic_ack(message.delivery.delivery_tag, multiple=up_to)

    async def reject(self, channel: Channel, message: DeliveredMessage, requeue: bool = False) -> Any:
        return await channel.basic_reject(message.delivery.delivery_tag, requeue=requeue)

    async def cancel(self, channel: Channel, consumer_tag: str) -> Any:
        return await channel.basic_cancel(consumer_tag)

    # -- consume ----------------------------------------------------------------

    async def consume(
        self,
        channel: Channel,
        queue_name: str,
        message_handler: MessageHandler,
        options: Optional[ConsumeOptions] = None,
        late_ack: bool = False,
        retry_options: Optional[ConsumeRetryOptions] = None,
        processing_timeout_ms: Optional[int] = None,
    ) -> str:
        """
        Start consuming ``queue_name`` on ``channel``.

        Every delivery runs the handler against the processing timeout, then
        settles: publishes the reply (unary or streaming), then acks. On a
        handler failure it climbs the retry ladder — park on the retry
        exchange, dead-letter after the last hop, answer the caller with the
        encoded error — or, for a handled error, answers and rejects without
        requeue. Everything that can raise runs before the ack, so a failed
        settlement leaves the message for redelivery rather than losing it.
        """
        options = options or ConsumeOptions()
        arity = _handler_arity(message_handler)
        connection = self

        async def on_message(msg: DeliveredMessage) -> None:
            properties = msg.header.properties
            delivery = msg.delivery
            reply_to = properties.reply_to
            correlation_id = properties.correlation_id or ""
            headers: Dict[str, Any] = dict(properties.headers or {})
            retry_count = header_int(headers, "x-retry-count", 0)
            routing_key = getattr(delivery, "routing_key", "") or ""
            original_routing_key = header_str(headers, "x-original-routing-key") or routing_key
            redelivered = bool(getattr(delivery, "redelivered", False))

            Logger.debug(
                f"incoming message on {queue_name}: {routing_key} ({correlation_id})"
                f"{f' (retry {retry_count})' if retry_count > 0 else ''}"
            )

            if not options.no_ack and not late_ack:
                # Early ackers never reject and immediately ack.
                await connection.ack(channel, msg)

            limit = processing_timeout_ms if processing_timeout_ms is not None else Config.message_processing_timeout()
            controller = AbortController()
            delivery_entry = _Delivery(controller=controller)
            group = connection._active_deliveries.setdefault(correlation_id, set())
            group.add(delivery_entry)

            context = MessageHandlerContext(
                signal=controller.signal,
                routing_key=routing_key,
                message_id=properties.message_id,
                redelivered=redelivered,
            )

            handler_task: Optional["asyncio.Task[Any]"] = None
            try:
                # Tracked around the race rather than around the await, so a
                # handler that outlives its own timeout is still counted as
                # running until it actually returns.
                connection._handler_started()
                try:
                    if arity >= 4:
                        awaitable = message_handler(msg.body, correlation_id, headers, context)
                    else:
                        awaitable = message_handler(msg.body, correlation_id, headers)
                    if not inspect.isawaitable(awaitable):
                        # A non-async handler already returned its result.
                        fut: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
                        fut.set_result(awaitable)
                        awaitable = fut
                    handler_task = asyncio.ensure_future(awaitable)
                except BaseException:
                    connection._handler_finished()
                    raise
                connection._handler_tasks.add(handler_task)
                handler_task.add_done_callback(lambda t: (connection._handler_tasks.discard(t), connection._handler_finished()))

                done, _pending = await asyncio.wait({handler_task}, timeout=limit / 1000)
                if not done:
                    controller.abort("processing timeout")
                    # The handler is cancelled rather than merely abandoned: a
                    # coroutine stops at its next await, which is the closest
                    # Python gets to interrupting it. A handler that swallows
                    # the cancellation runs on, still counted as running.
                    handler_task.cancel()
                    timeout_error = TimeoutError(f"message {correlation_id} exceeded the {limit}ms processing timeout")
                    if context.timeout_reply is not None:
                        setattr(timeout_error, RESPONSE_BUFFER_ATTR, context.timeout_reply)
                    raise timeout_error
                result = handler_task.result()

                # The reply is published before the request is settled, so the
                # worst case is a redelivered request (at-least-once) rather
                # than a settled request whose reply was never sent.
                if reply_to:
                    if _is_async_iterable(result):
                        await connection._publish_stream_reply(
                            channel, reply_to, correlation_id, result, lambda: delivery_entry.cancelled
                        )
                    elif isinstance(result, (bytes, bytearray)):
                        await connection.publish(
                            channel, Config.callbacks_exchange_name(), reply_to, bytes(result),
                            {"content_type": "application/octet-stream", "correlation_id": correlation_id},
                        )
                elif _is_async_iterable(result):
                    # Nothing to reply to; release whatever the generator holds.
                    await _aclose(result)
                if not options.no_ack and late_ack:
                    await connection.ack(channel, msg)

            except asyncio.CancelledError:
                raise
            except BaseException as err:
                if delivery_entry.cancelled:
                    # A cancelled delivery is a normal outcome: the caller
                    # asked to stop. Settle it so it is neither retried nor
                    # dead-lettered.
                    Logger.debug(f"message {correlation_id} ended because its stream was cancelled")
                    if not options.no_ack and late_ack:
                        await connection.ack(channel, msg)
                    return
                Logger.error(f"unhandled error consuming bus message - {err!r}")

                error_reply: Optional[bytes] = getattr(err, RESPONSE_BUFFER_ATTR, None)

                async def publish_error_reply() -> None:
                    # Best effort, deliberately: every terminal path answers
                    # the caller and THEN settles. A reply publish that fails
                    # must not take the settlement with it — the caller has a
                    # timeout, while the DLQ is the only durable record.
                    if not reply_to or error_reply is None:
                        return
                    try:
                        await connection.publish(
                            channel, Config.callbacks_exchange_name(), reply_to, error_reply,
                            {"content_type": "application/octet-stream", "correlation_id": correlation_id},
                        )
                    except Exception as reply_err:
                        Logger.error(
                            f"failed to publish the error reply for {correlation_id} to {reply_to}: "
                            f"{reply_err}. The caller will time out; settling the message anyway so it "
                            "reaches the DLQ."
                        )

                carried = carried_properties(properties)

                if not options.no_ack and late_ack:
                    is_handled = bool(retry_options.is_handled_error(err)) if retry_options and retry_options.is_handled_error else False

                    if retry_options and not is_handled and retry_options.max_retries > 0:
                        if retry_count < retry_options.max_retries:
                            # Park the message on the retry queue for delayed
                            # redelivery. The caller stays parked: no reply here.
                            new_retry_count = retry_count + 1
                            Logger.warn(f"retrying message {correlation_id} (attempt {new_retry_count}/{retry_options.max_retries})")
                            retry_headers = {
                                **headers,
                                "x-retry-count": new_retry_count,
                                "x-original-routing-key": original_routing_key,
                                "x-first-failure-time": headers.get("x-first-failure-time") or now_ms(),
                                "x-last-error": safe_error_summary(err),
                            }
                            retry_props = {
                                "persistent": True,
                                "correlation_id": correlation_id,
                                # Carried through so the retried copy is
                                # recognisable as the same logical message.
                                "message_id": properties.message_id,
                                "reply_to": reply_to,
                                "headers": retry_headers,
                                **carried,
                            }
                            if retry_options.retry_exchange_name:
                                await connection.publish(channel, retry_options.retry_exchange_name, original_routing_key, msg.body, retry_props)
                            else:
                                # Legacy: works for the first hop; the DLX redelivery drops.
                                await connection.publish_to_queue(channel, retry_options.retry_queue_name, msg.body, retry_props)
                            await connection.ack(channel, msg)
                        else:
                            Logger.error(f"message {correlation_id} exceeded max retries ({retry_options.max_retries}), sending to DLQ")
                            await publish_error_reply()
                            dlq_headers = {
                                **headers,
                                "x-retry-count": retry_count,
                                "x-original-routing-key": original_routing_key,
                                "x-original-queue": queue_name,
                                "x-first-failure-time": headers.get("x-first-failure-time") or now_ms(),
                                "x-dlq-time": now_ms(),
                                "x-last-error": safe_error_summary(err),
                            }
                            await connection.publish_to_queue(
                                channel, retry_options.dlq_name, msg.body,
                                {"persistent": True, "correlation_id": correlation_id, "message_id": properties.message_id, "headers": dlq_headers, **carried},
                            )
                            await connection.ack(channel, msg)
                    else:
                        if is_handled:
                            Logger.warn(f"handled error for message {correlation_id}, not retrying: {err}")
                        await publish_error_reply()
                        Logger.warn(f"rejecting message {correlation_id}")
                        await connection.reject(channel, msg, False)
                else:
                    # Early-ack (or no-ack) consumer: retry and DLQ are
                    # impossible, but the caller must still be told.
                    await publish_error_reply()
            finally:
                group = connection._active_deliveries.get(correlation_id)
                if group is not None:
                    group.discard(delivery_entry)
                    if not group:
                        connection._active_deliveries.pop(correlation_id, None)

        async def consumer_callback(msg: DeliveredMessage) -> None:
            # Counted for the whole settle so a graceful shutdown waits for the
            # reply/retry/DLQ publish, not just the handler body. A failure to
            # settle is swallowed: every path that can raise runs before the
            # ack, so the message stays unacknowledged and is redelivered.
            connection._delivery_started()
            try:
                await on_message(msg)
            except asyncio.CancelledError:
                raise
            except BaseException as err:
                Logger.error(
                    f"failed to settle message on {queue_name}: {err}. Leaving it unacknowledged for redelivery."
                )
            finally:
                connection._delivery_finished()

        result = await channel.basic_consume(
            queue_name,
            consumer_callback,
            no_ack=options.no_ack,
            exclusive=options.exclusive,
            consumer_tag=options.consumer_tag or None,
            arguments=options.arguments or None,
        )
        return result.consumer_tag

    # -- publish ----------------------------------------------------------------

    def _publish_state_for(self, channel: Any) -> _ChannelPublishState:
        try:
            state = self._publish_state.get(channel)
            if state is None:
                state = _ChannelPublishState()
                self._publish_state[channel] = state
                self._watch_channel_close(channel, state)
            return state
        except TypeError:
            state = self._publish_state_fallback.get(id(channel))
            if state is None:
                state = _ChannelPublishState()
                self._publish_state_fallback[id(channel)] = state
                self._watch_channel_close(channel, state)
            return state

    def _watch_channel_close(self, channel: Any, state: _ChannelPublishState) -> None:
        closing = getattr(channel, "closing", None)
        if closing is None or not hasattr(closing, "add_done_callback"):
            return

        def fail_all(_future: Any) -> None:
            # A channel closing with confirms outstanding leaves those
            # messages in an UNKNOWN state, which the caller must be told.
            pending, state.pending = list(state.pending), set()
            for fut in pending:
                if not fut.done():
                    # A cancelled confirm task is reported to its publisher
                    # as ChannelClosedError by _confirmed_publish.
                    fut.cancel()
            # Release anyone parked on the in-flight bound.
            waiters, state.waiters = state.waiters, []
            for w in waiters:
                if not w.done():
                    w.set_result(None)

        closing.add_done_callback(fail_all)

    async def publish(self, channel: Channel, exchange_name: str, routing_key: str, content: bytes, properties: Optional[Dict[str, Any]] = None) -> None:
        """
        Publish and return only once RabbitMQ has confirmed the message.

        A normal return means the broker positively confirmed the publication
        and, when ``mandatory`` asked for routing to be enforced, that it was
        routed. Everything else is a typed exception — see PublishError and
        subclasses. PublishConfirmTimeoutError and ChannelClosedError mean the
        outcome is UNKNOWN, not failed: retrying either can duplicate the
        message, which is why every publish carries a stable ``message_id``.
        """
        await self._confirmed_publish(
            channel, properties or {}, content,
            lambda props, mandatory: channel.basic_publish(content, exchange=exchange_name, routing_key=routing_key, properties=props, mandatory=mandatory),
            f"{exchange_name or '(default)'} -> {routing_key}",
        )

    async def publish_to_queue(self, channel: Channel, queue_name: str, content: bytes, properties: Optional[Dict[str, Any]] = None) -> None:
        """Publish straight to a queue (via the default exchange) with the same
        confirm guarantee as publish(). Used by the retry and DLQ paths."""
        await self._confirmed_publish(
            channel, properties or {}, content,
            lambda props, mandatory: channel.basic_publish(content, exchange="", routing_key=queue_name, properties=props, mandatory=mandatory),
            f"queue {queue_name}",
        )

    async def _confirmed_publish(
        self,
        channel: Channel,
        properties: Dict[str, Any],
        content: bytes,
        send: Callable[[Basic.Properties, bool], Awaitable[Any]],
        describe: str,
    ) -> None:
        state = self._publish_state_for(channel)
        # A caller-supplied message_id survives retries, which is what lets a
        # consumer recognise a duplicate after an ambiguous outcome.
        message_id = properties.get("message_id") or properties.get("messageId") or str(uuid.uuid4())
        mandatory = bool(properties.get("mandatory", False))
        props = build_properties({**properties, "message_id": message_id})

        # Bound unconfirmed work before touching the channel at all.
        while state.in_flight >= Config.max_outstanding_confirms():
            waiter: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
            state.waiters.append(waiter)
            try:
                await waiter
            finally:
                try:
                    state.waiters.remove(waiter)
                except ValueError:
                    pass
        state.in_flight += 1

        timeout_ms = Config.publish_confirm_timeout_ms()
        loop = asyncio.get_running_loop()
        confirm: "asyncio.Future[Any]" = asyncio.ensure_future(send(props, mandatory), loop=loop)
        state.pending.add(confirm)
        try:
            try:
                done, _ = await asyncio.wait({confirm}, timeout=timeout_ms / 1000)
            except asyncio.CancelledError:
                confirm.cancel()
                raise
            if not done:
                confirm.cancel()
                raise PublishConfirmTimeoutError(f"no broker confirm for {describe} within {timeout_ms}ms", message_id)
            if confirm.cancelled():
                raise ChannelClosedError(f"{describe} was unconfirmed when the channel closed", message_id)
            exc = confirm.exception()
            if exc is not None:
                raise _classify_publish_failure(exc, describe, message_id)
            result = confirm.result()
            if isinstance(result, DeliveredMessage) or isinstance(getattr(result, "delivery", None), Basic.Return):
                # Confirmed, but returned first: it reached no queue.
                raise UnroutableError(f"{describe} was confirmed but returned as unroutable", message_id)
            if isinstance(result, Basic.Nack):
                raise PublishNackedError(f"broker nacked {describe}", message_id)
        finally:
            state.pending.discard(confirm)
            state.in_flight -= 1
            while state.waiters:
                nxt = state.waiters.pop(0)
                if not nxt.done():
                    nxt.set_result(None)
                    break

    async def _publish_stream_reply(
        self,
        channel: Channel,
        reply_to: str,
        correlation_id: str,
        chunks: AsyncIterable[bytes],
        is_cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        """
        Publish a streaming reply: every chunk carries the same correlation
        id; all but the last carry ``x-protobus-final=false``, the last
        ``true``. An empty stream publishes a single empty terminal message.
        Look-ahead-by-one avoids an extra terminal in the common case.
        """

        async def publish_one(body: bytes, seq: int, final: bool) -> None:
            await self.publish(
                channel, Config.callbacks_exchange_name(), reply_to, body,
                {
                    "content_type": "application/octet-stream",
                    "correlation_id": correlation_id,
                    "headers": {Config.HEADER_FINAL: final, Config.HEADER_SEQ: seq},
                },
            )

        seq = 0
        buffered: Optional[bytes] = None
        iterator = chunks.__aiter__()
        try:
            while True:
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                # A caller that cancelled is not listening: stop sending and
                # stop pulling, closing the generator so a cooperative producer
                # releases whatever it holds open.
                if is_cancelled():
                    Logger.debug(f"stream {correlation_id} cancelled after {seq} chunk(s); not publishing further")
                    return
                if buffered is not None:
                    await publish_one(buffered, seq, False)
                    seq += 1
                buffered = bytes(chunk)
        finally:
            await _aclose(iterator)

        if is_cancelled():
            return
        if buffered is not None:
            await publish_one(buffered, seq, True)
        else:
            await publish_one(b"", 0, True)


def _classify_publish_failure(exc: BaseException, describe: str, message_id: str) -> Exception:
    if isinstance(exc, (PublishNackedError, UnroutableError, ChannelClosedError, PublishConfirmTimeoutError)):
        return exc
    frame = getattr(exc, "frame", None)
    if isinstance(exc, aiormq.exceptions.DeliveryError) and isinstance(frame, Basic.Nack):
        return PublishNackedError(f"broker nacked {describe}: {exc}", message_id)
    if isinstance(exc, aiormq.exceptions.DeliveryError) and (isinstance(frame, Basic.Return) or isinstance(getattr(exc, "message", None), DeliveredMessage)):
        return UnroutableError(f"{describe} was confirmed but returned as unroutable", message_id)
    text = str(exc) or type(exc).__name__
    if isinstance(exc, (aiormq.exceptions.ChannelInvalidStateError, aiormq.exceptions.ConnectionClosed, aiormq.exceptions.ChannelClosed, ConnectionError)) or "closed" in text.lower():
        return ChannelClosedError(f"{describe} was unconfirmed when the channel closed: {text}", message_id)
    return PublishNackedError(f"publish of {describe} failed: {text}", message_id)


async def _aclose(iterator: Any) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as err:
        Logger.debug(f"error closing stream iterator: {err}")
