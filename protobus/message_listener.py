"""Message listener with retry queue functionality."""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from aio_pika import ExchangeType

from .base_listener import BaseListener
from .config import Config
from .connection import IConnection, RetryOptions
from .logger import Logger


@dataclass
class RetryConfig:
    """Configuration for message retry behavior."""

    max_retries: int = 3
    retry_delay_ms: int = 5000
    message_ttl_ms: Optional[int] = None


class MessageListener(BaseListener):
    """
    Listener for service messages with retry queue support.

    Creates retry and dead-letter queues when retries are enabled.
    Uses a topic exchange for routing.
    """

    def __init__(
        self,
        connection: IConnection,
        late_ack: bool = False,
        max_concurrent: Optional[int] = None,
        retry_options: Optional[RetryOptions] = None,
    ):
        """
        Initialize the message listener.

        Args:
            connection: The connection to use
            late_ack: Whether to use late acknowledgment
            max_concurrent: Maximum concurrent messages (prefetch count)
            retry_options: Options for retry behavior
        """
        message_ttl = None
        if retry_options and retry_options.message_ttl_ms:
            message_ttl = retry_options.message_ttl_ms

        super().__init__(
            connection,
            late_ack=late_ack,
            max_concurrent=max_concurrent,
            message_ttl_ms=message_ttl,
        )

        self._exchange_name = Config.bus_exchange_name()
        self._exchange_type = ExchangeType.TOPIC
        self._retry_config = RetryConfig(
            max_retries=retry_options.max_retries if retry_options else 3,
            retry_delay_ms=retry_options.retry_delay_ms if retry_options else 5000,
            message_ttl_ms=message_ttl,
        )
        self._retry_queue_setup = False

    def get_retry_queue_name(self) -> str:
        """Get the retry queue name."""
        return f"{self._queue_name}.retry" if self._queue_name else ""

    def get_dlq_name(self) -> str:
        """Get the dead letter queue name."""
        return f"{self._queue_name}.DLQ" if self._queue_name else ""

    def get_retry_config(self) -> RetryConfig:
        """Get the retry configuration."""
        return self._retry_config

    def get_retry_options(self) -> Optional[RetryOptions]:
        """Get retry options for consumption."""
        if self._retry_config.max_retries <= 0:
            return None
        return RetryOptions(
            max_retries=self._retry_config.max_retries,
            retry_delay_ms=self._retry_config.retry_delay_ms,
            message_ttl_ms=self._retry_config.message_ttl_ms,
        )

    async def _setup_retry_queues(self) -> None:
        """Set up retry and dead letter queues."""
        if (
            self._retry_queue_setup
            or self._retry_config.max_retries <= 0
            or not self._queue_name  # Anonymous queues don't use retry
            or not self._channel
            or not self._exchange
        ):
            return

        try:
            # Create DLQ
            dlq_name = self.get_dlq_name()
            await self._connection.ensure_queue(self._channel, dlq_name)
            Logger.debug(f"Created DLQ: {dlq_name}")

            # Create retry queue with TTL that routes back to main exchange
            retry_queue_name = self.get_retry_queue_name()
            retry_arguments = {
                "x-message-ttl": self._retry_config.retry_delay_ms,
                "x-dead-letter-exchange": self._exchange_name,
                "x-dead-letter-routing-key": f"REQUEST.{self._queue_name}.*",
            }
            retry_queue = await self._connection.ensure_queue(
                self._channel, retry_queue_name, arguments=retry_arguments
            )
            Logger.debug(f"Created retry queue: {retry_queue_name}")

            self._retry_queue_setup = True

        except Exception as e:
            Logger.error(f"Failed to set up retry queues: {e}")

    async def subscribe(self, routing_key: str) -> None:
        """
        Subscribe to messages matching a routing key pattern.

        Also sets up retry queues if not already done.

        Args:
            routing_key: Routing key pattern to subscribe to
        """
        await super().subscribe(routing_key)
        await self._setup_retry_queues()
