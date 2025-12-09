"""Message dispatcher for RPC communication."""

import asyncio
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from aio_pika import ExchangeType, Message
from aio_pika.abc import AbstractChannel, AbstractExchange

from .callback_listener import CallbackListener
from .config import Config
from .connection import IConnection
from .errors import DisconnectedError, NotConnectedError, NotInitializedError
from .logger import Logger


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
        self._callback_listener: Optional[CallbackListener] = None
        self._pending_callbacks: Dict[str, asyncio.Future] = {}
        self._is_initialized = False

        # Set up connection event handlers
        self._connection.on("reconnected", self._on_reconnected)
        self._connection.on("disconnected", self._on_disconnected)

    @property
    def is_initialized(self) -> bool:
        """Check if the dispatcher has been initialized."""
        return self._is_initialized

    async def init(self) -> None:
        """Initialize the message dispatcher."""
        await self._setup_channel()
        self._is_initialized = True

    async def _setup_channel(self) -> None:
        """Set up the channel and callback listener."""
        self._channel = await self._connection.open_channel()

        # Declare the main exchange
        self._exchange = await self._connection.ensure_exchange(
            self._channel,
            Config.bus_exchange_name(),
            ExchangeType.TOPIC,
        )

        # Set up callback listener for RPC responses
        self._callback_listener = CallbackListener(self._connection)
        await self._callback_listener.init(self._on_result, "")

        # Bind the callback queue to the callback exchange
        if self._callback_listener._queue and self._callback_listener._exchange:
            await self._connection.bind_queue(
                self._callback_listener._queue,
                self._callback_listener._exchange,
                self._callback_listener.callback_queue,
            )

        await self._callback_listener.start()
        Logger.debug("MessageDispatcher initialized")

    async def _on_result(self, data: bytes, correlation_id: str) -> Optional[bytes]:
        """
        Handle incoming RPC responses.

        Args:
            data: Response data
            correlation_id: Correlation ID to match with pending request
        """
        if correlation_id in self._pending_callbacks:
            future = self._pending_callbacks.pop(correlation_id)
            if not future.done():
                future.set_result(data)
        else:
            Logger.warn(f"Received response for unknown correlation ID: {correlation_id}")
        return None

    async def publish(
        self,
        data: bytes,
        routing_key: str,
        rpc: bool = True,
        timeout_ms: Optional[int] = None,
    ) -> Optional[bytes]:
        """
        Publish a message and optionally wait for a response.

        Args:
            data: Message data to publish
            routing_key: Routing key for the message
            rpc: Whether to wait for a response
            timeout_ms: Timeout for RPC response in milliseconds

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

        correlation_id = str(uuid.uuid4())

        # Set up response future if RPC
        response_future: Optional[asyncio.Future] = None
        if rpc and self._callback_listener:
            response_future = asyncio.get_event_loop().create_future()
            self._pending_callbacks[correlation_id] = response_future

        try:
            # Build message properties
            reply_to = self._callback_listener.callback_queue if rpc else None

            message = Message(
                body=data,
                correlation_id=correlation_id,
                reply_to=reply_to,
            )

            await self._exchange.publish(message, routing_key=routing_key)
            Logger.debug(f"Published message to {routing_key}")

            # Wait for response if RPC
            if rpc and response_future:
                timeout = (timeout_ms or Config.message_processing_timeout()) / 1000
                try:
                    return await asyncio.wait_for(response_future, timeout=timeout)
                except asyncio.TimeoutError:
                    # Clean up the pending callback
                    self._pending_callbacks.pop(correlation_id, None)
                    raise

            return None

        except Exception as e:
            # Clean up on error
            self._pending_callbacks.pop(correlation_id, None)
            raise

    async def _on_reconnected(self) -> None:
        """Handle reconnection event."""
        Logger.debug("MessageDispatcher reconnecting...")
        try:
            await self._setup_channel()
            Logger.debug("MessageDispatcher reconnected")
        except Exception as e:
            Logger.error(f"Error reconnecting MessageDispatcher: {e}")

    def _on_disconnected(self) -> None:
        """Handle disconnection event."""
        Logger.debug("MessageDispatcher disconnected")

        # Fail all pending callbacks
        error = DisconnectedError("Connection lost while waiting for response")
        for correlation_id, future in list(self._pending_callbacks.items()):
            if not future.done():
                future.set_exception(error)
        self._pending_callbacks.clear()

        self._channel = None
        self._exchange = None
