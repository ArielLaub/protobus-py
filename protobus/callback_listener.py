"""Listener for RPC replies: an anonymous exclusive queue on the callbacks
(direct) exchange, bound to its own name."""

from .base_listener import BaseListener
from .config import Config
from .connection import IConnection


class CallbackListener(BaseListener):
    def __init__(self, connection: IConnection) -> None:
        super().__init__(connection)
        self._exchange_name = Config.callbacks_exchange_name()
        self._exchange_type = "direct"

    @property
    def callback_queue(self) -> str:
        return self._queue_name
