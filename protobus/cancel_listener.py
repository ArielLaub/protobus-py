"""
Listens for stream-cancellation notices and stops the matching in-flight
stream in this process.

Deliberately NOT a BaseListener. It needs its own channel and an unbounded
prefetch: the whole point is to be heard while the service is busy inside a
streaming handler, and BaseListener's request-serving machinery (retry
queues, DLQ, late ack, prefetch of 1) would make it queue behind exactly the
work it is meant to interrupt.

The queue is exclusive and auto-delete, so each replica has its own and the
broker cleans it up when the process goes away. Cancels are consumed with
no_ack — a lost cancel means the stream runs on, which is the same outcome
as never having sent one.
"""

from typing import Any, Callable, Optional

from .config import Config
from .connection import EXCHANGE_OPTIONS, IConnection, attach_restorer
from .logger import Logger


class CancelListener:
    def __init__(self, connection: IConnection) -> None:
        self._connection = connection
        self._channel: Any = None
        self._queue_name = ""
        self._consumer_tag = ""
        self._detach_restorer: Callable[[], None] = attach_restorer(connection, self._restore, "CancelListener")

    @property
    def queue_name(self) -> str:
        return self._queue_name

    async def start(self) -> None:
        """
        Best effort by design: a deployment whose broker credentials cannot
        declare the cancel exchange keeps working without cancellation
        support rather than failing to start.
        """
        try:
            await self._start()
        except Exception as err:
            Logger.warn(
                f"CancelListener: stream cancellation unavailable ({err}). "
                "Streams will run to completion; everything else is unaffected."
            )
            self._channel = None

    async def _start(self) -> None:
        self._channel = await self._connection.open_channel()
        await self._connection.declare_exchange(
            self._channel, Config.cancel_exchange_name(), "fanout",
            dict(EXCHANGE_OPTIONS),
        )
        # Anonymous queue: the broker names it, this process owns it, and it
        # disappears with the connection.
        self._queue_name = await self._connection.declare_queue(
            self._channel, "", {"exclusive": True, "auto_delete": True, "durable": False}
        )
        await self._connection.bind_queue(self._channel, self._queue_name, Config.cancel_exchange_name(), "", {})

        cancel_stream: Optional[Callable[[str], bool]] = getattr(self._connection, "cancel_stream", None)

        async def on_cancel(msg: Any) -> None:
            properties = getattr(getattr(msg, "header", None), "properties", None)
            correlation_id = getattr(properties, "correlation_id", None)
            if not correlation_id:
                return
            # Every replica sees every cancel; only the one running that
            # stream has anything to do.
            if callable(cancel_stream):
                cancel_stream(correlation_id)

        result = await self._channel.basic_consume(self._queue_name, on_cancel, no_ack=True)
        self._consumer_tag = getattr(result, "consumer_tag", "") or ""
        Logger.debug(f"CancelListener: consuming cancellations on {self._queue_name}")

    async def _restore(self, _generation: int = 0) -> None:
        """Never raises: cancellation is the one thing a deployment can run without."""
        if self._channel is None and not self._queue_name:
            return
        await self.start()
        Logger.debug("CancelListener: re-established after reconnection")

    async def close(self) -> None:
        self._detach_restorer()
        if self._channel is not None and self._connection.is_connected:
            try:
                if self._consumer_tag:
                    await self._connection.cancel(self._channel, self._consumer_tag)
                await self._connection.close_channel(self._channel)
            except Exception as err:
                Logger.debug(f"CancelListener: error during close: {err}")
        self._channel = None
        self._consumer_tag = ""
        self._queue_name = ""
