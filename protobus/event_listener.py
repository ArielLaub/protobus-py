"""Event listener: topic-routed subscriptions on the events exchange."""

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from .base_listener import BaseListener
from .config import Config
from .connection import ConsumeRetryOptions, IConnection, MessageHandler, MessageHandlerContext
from .errors import RetryQueueMismatchError, is_handled_error
from .logger import Logger
from .message_factory import MessageFactory
from .message_listener import is_precondition_failed
from .trie import Trie

EventHandler = Callable[..., Awaitable[None]]
"""
``async def handler(event, type, topic)``. A handler declaring two positional
parameters is called as ``handler(event, topic)`` and one as ``handler(event)``.
"""


@dataclass
class EventRetryOptions:
    """
    Opt-in retry for event handlers. Left unset, a handler that raises loses
    its event — the delivery is rejected without requeue, which is what stops
    one permanently-failing event from stalling the subscriber behind its own
    prefetch. Enabling it gives events the ladder RPC requests already climb:
    park, wait, redeliver, and dead-letter once the attempts are spent.
    """

    max_retries: int = 0  # 0 keeps the drop-on-failure behaviour
    retry_delay_ms: int = 5000


def _call_event_handler(handler: Callable[..., Any], data: Any, event_type: str, topic: str) -> Any:
    arity = _arity(handler)
    if arity >= 3:
        return handler(data, event_type, topic)
    if arity == 2:
        return handler(data, topic)
    return handler(data)


def _arity(handler: Callable[..., Any]) -> int:
    try:
        params = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        return 3
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return 3
    return len([p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)])


class EventListener(BaseListener):
    """
    Late-ack consumer of a service's ``<Service>.Events`` queue. Handlers are
    matched by the routing key the broker delivered on, through a Trie that
    understands ``*`` and ``#``.
    """

    def __init__(
        self,
        connection: IConnection,
        message_factory: MessageFactory,
        retry: Optional[EventRetryOptions] = None,
    ) -> None:
        super().__init__(connection)
        retry = retry or EventRetryOptions()
        self._retry_config = EventRetryOptions(
            max_retries=retry.max_retries or 0,
            retry_delay_ms=retry.retry_delay_ms if retry.retry_delay_ms is not None else 5000,
        )
        self._router: Trie = Trie()
        self._exchange_name = Config.events_exchange_name()
        self._exchange_type = "topic"
        self._late_ack = True
        self._all_handler: Optional[EventHandler] = None
        self._message_factory = message_factory
        self._retry_queue_name = ""
        self._retry_exchange_name = ""
        self._redelivery_exchange_name = ""
        self._dlq_name = ""

        async def default_handler(
            encoded_event: bytes,
            _correlation_id: str = "",
            _headers: Optional[Dict[str, Any]] = None,
            context: Optional[MessageHandlerContext] = None,
        ) -> None:
            event = self._message_factory.decode_event(encoded_event)
            if self._all_handler is not None:
                await _await(_call_event_handler(self._all_handler, event.data, event.type, event.topic))
            # Prefer the routing key the broker actually delivered on over the
            # topic carried in the body. They agree for anything published by
            # EventDispatcher, but the body is publisher-controlled, so trusting
            # it would let a publisher target handlers its routing key was
            # never permitted to reach.
            match_topic = (context.routing_key if context is not None else None) or event.topic
            if match_topic:
                handlers = self._router.match_topic(match_topic)
                for handler in handlers:
                    await _await(_call_event_handler(handler, event.data, event.type, event.topic))
            else:
                # Type only: event.data is application payload.
                Logger.warn(f"ignoring unhandled event of type '{event.type or 'unknown'}' (no topic to route on)")

        self._default_handler = default_handler

    async def setup_retry_topology(self) -> None:
        """
        Declare the retry ladder, after the queue this listener consumes exists.

        A request's retry queue dead-letters back to ``proto.bus``, which
        routes to exactly one service queue. Events fan out: dead-lettering
        back to ``proto.bus.events`` would redeliver to every subscriber bound
        to the topic, including the ones that handled it. So the expired
        message goes to a per-subscriber topic exchange bound only to this
        listener's own queue.
        """
        if self._retry_config.max_retries <= 0 or self._is_anonymous:
            return
        base = self._configured_queue_name
        self._dlq_name = f"{base}.DLQ"
        self._retry_queue_name = f"{base}.Retry"
        self._retry_exchange_name = f"{base}.Retry.Exchange"
        self._redelivery_exchange_name = f"{base}.Redelivery"

        await self._connection.declare_queue(
            self._channel, self._dlq_name,
            {"durable": True, "auto_delete": False, "exclusive": False, "arguments": {}},
        )
        await self._connection.declare_exchange(
            self._channel, self._redelivery_exchange_name, "topic",
            {"durable": True, "auto_delete": False, "internal": False, "arguments": {}},
        )
        await self._connection.bind_queue(self._channel, self._queue_name, self._redelivery_exchange_name, "#", {})

        try:
            await self._connection.declare_queue(
                self._channel, self._retry_queue_name,
                {
                    "durable": True, "auto_delete": False, "exclusive": False,
                    "arguments": {
                        "x-message-ttl": self._retry_config.retry_delay_ms,
                        "x-dead-letter-exchange": self._redelivery_exchange_name,
                    },
                },
            )
        except Exception as error:
            if is_precondition_failed(error):
                raise RetryQueueMismatchError(
                    f"event retry queue '{self._retry_queue_name}' already exists with different "
                    f"arguments (most likely a different retry_delay_ms — now "
                    f"{self._retry_config.retry_delay_ms}ms). RabbitMQ cannot change a queue's "
                    "x-message-ttl in place: drain and delete the queue, or keep the original "
                    f"retry_delay_ms. Original error: {error}"
                ) from error
            raise

        await self._connection.declare_exchange(
            self._channel, self._retry_exchange_name, "topic",
            {"durable": True, "auto_delete": False, "internal": False, "arguments": {}},
        )
        await self._connection.bind_queue(self._channel, self._retry_queue_name, self._retry_exchange_name, "#", {})

    def get_retry_options(self) -> Optional[ConsumeRetryOptions]:
        if self._retry_config.max_retries <= 0 or not self._retry_queue_name or not self._dlq_name:
            return None
        return ConsumeRetryOptions(
            max_retries=self._retry_config.max_retries,
            retry_queue_name=self._retry_queue_name,
            retry_exchange_name=self._retry_exchange_name,
            dlq_name=self._dlq_name,
            is_handled_error=is_handled_error,
        )

    async def init(self, message_handler: Optional[MessageHandler], queue_name: str = "") -> None:
        if self.is_initialized:
            return
        await super().init(message_handler, queue_name)
        # Before start(), so the first delivery already has somewhere to fail to.
        await self.setup_retry_topology()

    async def subscribe(self, event_type: str, handler: EventHandler, topic: Optional[str] = None) -> Any:
        if not topic:
            topic = f"EVENT.{event_type}"
        self._router.add_match(topic, handler)
        self.track_binding(topic)
        return await self._connection.bind_queue(self._channel, self._queue_name, self._exchange_name, topic, {})

    async def subscribe_all(self, handler: EventHandler) -> Any:
        self._all_handler = handler
        self.track_binding("#")
        return await self._connection.bind_queue(self._channel, self._queue_name, self._exchange_name, "#", {})

    @property
    def retry_topology(self) -> Optional[Dict[str, str]]:
        """Names of the retry objects, for tests and operators. None when retry is off."""
        if not self._retry_queue_name:
            return None
        return {"retry_queue": self._retry_queue_name, "dlq": self._dlq_name}


async def _await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value
