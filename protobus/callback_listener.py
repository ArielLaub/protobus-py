"""Callback listener for RPC response handling."""

from aio_pika import ExchangeType

from .base_listener import BaseListener
from .config import Config
from .connection import IConnection


class CallbackListener(BaseListener):
    """
    Listener for callback/RPC response messages.

    Uses a direct exchange for routing responses back to callers.
    """

    def __init__(self, connection: IConnection):
        """
        Initialize the callback listener.

        Args:
            connection: The connection to use
        """
        super().__init__(connection, late_ack=False, max_concurrent=None)
        self._exchange_name = Config.callbacks_exchange_name()
        self._exchange_type = ExchangeType.DIRECT

    @property
    def callback_queue(self) -> str:
        """Get the callback queue name."""
        return self._queue_name
