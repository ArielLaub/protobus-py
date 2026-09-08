"""Listener for a service's request queue, with the retry / DLQ ladder."""

import re
from dataclasses import dataclass
from typing import List, Optional, Union

from .base_listener import BaseListener
from .config import Config
from .connection import ConsumeRetryOptions, IConnection
from .errors import InvalidPriorityError, RetryQueueMismatchError, is_handled_error
from .priority import validate_max_priority


@dataclass
class RetryOptions:
    """Retry for a service's request queue."""

    max_retries: int = 3  # 0 = no retries
    retry_delay_ms: int = 5000
    message_ttl_ms: Optional[int] = None  # None = no TTL on the main queue


DEFAULT_RETRY_OPTIONS = RetryOptions()


@dataclass
class RetryConfig:
    max_retries: int
    retry_delay_ms: int
    message_ttl_ms: Optional[int] = None


_PRECONDITION_FAILED = re.compile(r"PRECONDITION[_-]FAILED", re.IGNORECASE)


def is_precondition_failed(error: BaseException) -> bool:
    """True for RabbitMQ's 406, which is how a changed queue argument fails."""
    if type(error).__name__ == "ChannelPreconditionFailed":
        return True
    return bool(_PRECONDITION_FAILED.search(str(error))) or getattr(error, "code", None) == 406


class MessageListener(BaseListener):
    """
    Consumes ``REQUEST.<service>.*`` from the bus exchange on a durable,
    named queue, and declares the retry queue, retry exchange and DLQ beside
    it.

    The retry exchange is what makes redelivery work: the retry queue is
    bound to it with ``#``, so a message parked there keeps its original
    ``REQUEST.<service>.<method>`` routing key, and when the queue's TTL
    dead-letters it back to the bus exchange that key still routes it to the
    main queue. Publishing straight to the queue would replace the key with
    the queue name and the redelivery would drop.
    """

    def __init__(
        self,
        connection: IConnection,
        late_ack: bool = False,
        max_concurrent: Optional[int] = None,
        retry_options: Optional[RetryOptions] = None,
        processing_timeout_ms: Optional[int] = None,
        max_priority: Optional[int] = None,
    ) -> None:
        super().__init__(connection)
        # Validated at construction, before any broker I/O, so a bad value
        # fails here rather than as a 406 that closes the channel.
        self._max_priority = validate_max_priority(max_priority)

        # Priority can only reorder messages still in the QUEUE. An early-ack
        # consumer has no prefetch, so the broker pushes the whole backlog and
        # there is nothing left to reorder. Refused, because the failure is
        # otherwise invisible.
        if self._max_priority is not None and not late_ack:
            raise InvalidPriorityError(
                "max_priority requires late_ack. With late_ack off the consumer acks on delivery, "
                "RabbitMQ applies no prefetch, and the broker hands it the entire backlog — leaving "
                "priority nothing to reorder. MessageService enables late_ack by default, so this "
                "means an explicit late_ack=False; MessageListener, constructed directly, defaults "
                "it to off and needs it passed. Enable late_ack, or drop max_priority."
            )

        self._exchange_name = Config.bus_exchange_name()
        self._exchange_type = "topic"
        self._late_ack = bool(late_ack)
        self._max_concurrent = max_concurrent or 1
        self._processing_timeout_ms = processing_timeout_ms

        self._retry_config = RetryConfig(
            max_retries=retry_options.max_retries if retry_options and retry_options.max_retries is not None else DEFAULT_RETRY_OPTIONS.max_retries,
            retry_delay_ms=retry_options.retry_delay_ms if retry_options and retry_options.retry_delay_ms is not None else DEFAULT_RETRY_OPTIONS.retry_delay_ms,
            message_ttl_ms=retry_options.message_ttl_ms if retry_options else None,
        )
        self._message_ttl_ms = retry_options.message_ttl_ms if retry_options else None

        self._dlq_name = ""
        self._retry_queue_name = ""
        self._retry_exchange_name = ""

    async def setup_retry_queues(self) -> None:
        """Declare the DLQ, retry queue and retry exchange. Called after the
        main queue's bindings, from subscribe()."""
        if self._retry_config.max_retries <= 0 or self._is_anonymous:
            return

        service_name = self._configured_queue_name

        self._dlq_name = f"{service_name}.DLQ"
        await self._connection.declare_queue(
            self._channel, self._dlq_name,
            {"durable": True, "auto_delete": False, "exclusive": False, "arguments": {}},
        )

        self._retry_queue_name = f"{service_name}.Retry"
        try:
            await self._connection.declare_queue(
                self._channel, self._retry_queue_name,
                {
                    "durable": True, "auto_delete": False, "exclusive": False,
                    "arguments": {
                        "x-message-ttl": self._retry_config.retry_delay_ms,
                        "x-dead-letter-exchange": self._exchange_name,
                        # No x-dead-letter-routing-key: the message's *own*
                        # key is preserved, by publishing to the retry
                        # exchange with it.
                    },
                },
            )
        except Exception as error:
            # retry_delay_ms becomes the queue's x-message-ttl, and RabbitMQ
            # fixes queue arguments at declare time. Say what has to happen.
            if is_precondition_failed(error):
                raise RetryQueueMismatchError(
                    f"retry queue '{self._retry_queue_name}' already exists with different arguments "
                    f"(most likely a different retry_delay_ms — now {self._retry_config.retry_delay_ms}ms). "
                    "RabbitMQ cannot change a queue's x-message-ttl in place: drain and delete the "
                    f"queue, or keep the original retry_delay_ms. Original error: {error}"
                ) from error
            raise

        self._retry_exchange_name = f"{service_name}.Retry.Exchange"
        await self._connection.declare_exchange(
            self._channel, self._retry_exchange_name, "topic",
            {"durable": True, "auto_delete": False, "internal": False, "arguments": {}},
        )
        await self._connection.bind_queue(self._channel, self._retry_queue_name, self._retry_exchange_name, "#", {})

    def get_retry_queue_name(self) -> str:
        return self._retry_queue_name

    def get_dlq_name(self) -> str:
        return self._dlq_name

    def get_retry_exchange_name(self) -> str:
        return self._retry_exchange_name

    def get_retry_config(self) -> RetryConfig:
        return self._retry_config

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

    async def subscribe(self, topics: Union[str, List[str]]) -> None:
        if isinstance(topics, str):
            topics = [topics]
        for topic in topics:
            await self._connection.bind_queue(self._channel, self._queue_name, self._exchange_name, topic, {})
            self.track_binding(topic)
        # Retry queues after the main queue's bindings.
        await self.setup_retry_queues()
