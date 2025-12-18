"""Runnable service - base class for services with lifecycle management."""

import asyncio
import signal
from abc import abstractmethod
from typing import Any, Callable, Optional, Type, TypeVar

from .context import Context, IContext
from .logger import Logger
from .message_service import MessageService, MessageServiceOptions

T = TypeVar("T", bound="RunnableService")


class RunnableService(MessageService):
    """
    Abstract base class for services with lifecycle management.

    Extends MessageService with:
    - Automatic proto filename resolution from service name
    - Graceful shutdown handling (SIGINT, SIGTERM)
    - Static bootstrap method for easy service startup
    - Cleanup hook for custom shutdown logic

    Example:
        class CalculatorService(RunnableService):
            @property
            def service_name(self) -> str:
                return "Calculator.Service"

            async def add(self, data: dict, actor: str, correlation_id: str) -> dict:
                return {"result": data["a"] + data["b"]}

        # Start the service
        asyncio.run(CalculatorService.start(ctx, CalculatorService))
    """

    def __init__(
        self,
        context: IContext,
        options: Optional[MessageServiceOptions] = None,
    ):
        """
        Initialize the runnable service.

        Args:
            context: The context to use for messaging
            options: Optional service configuration
        """
        super().__init__(context, options)
        self._shutdown_event: Optional[asyncio.Event] = None
        self._shutdown_handlers_installed = False

    @property
    @abstractmethod
    def service_name(self) -> str:
        """Get the service name."""
        ...

    @property
    def proto_file_name(self) -> str:
        """
        Get the proto file name.

        Derives the proto filename from service_name by convention:
        - "Calculator.Service" -> "Calculator.proto"
        - "combat.Player" -> "combat.proto"
        """
        # Take the first part of the service name as the proto filename
        parts = self.service_name.split(".")
        return f"{parts[0]}.proto"

    async def cleanup(self) -> None:
        """
        Clean up resources on shutdown.

        Override this method to implement custom cleanup logic,
        such as closing database connections or flushing caches.
        """
        pass

    def _setup_signal_handlers(self) -> None:
        """Set up signal handlers for graceful shutdown."""
        if self._shutdown_handlers_installed:
            return

        loop = asyncio.get_event_loop()

        def signal_handler(sig: signal.Signals) -> None:
            Logger.info(f"Received {sig.name}, shutting down gracefully...")
            if self._shutdown_event:
                self._shutdown_event.set()

        # Register signal handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))
            except NotImplementedError:
                # Signal handlers not available on Windows
                signal.signal(sig, lambda s, f: signal_handler(signal.Signals(s)))

        self._shutdown_handlers_installed = True

    async def run(self) -> None:
        """
        Run the service until shutdown signal is received.

        This method blocks until SIGINT or SIGTERM is received,
        then performs graceful shutdown.
        """
        self._shutdown_event = asyncio.Event()
        self._setup_signal_handlers()

        Logger.info(f"Service {self.service_name} running, press Ctrl+C to stop")

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        # Perform cleanup
        Logger.info(f"Cleaning up service {self.service_name}...")
        await self.cleanup()
        Logger.info(f"Service {self.service_name} stopped")

    @classmethod
    async def start(
        cls: Type[T],
        context: IContext,
        service_class: Optional[Type[T]] = None,
        options: Optional[MessageServiceOptions] = None,
        post_init: Optional[Callable[[T], Any]] = None,
    ) -> T:
        """
        Bootstrap and start a service.

        This is a convenience method for starting a service with
        proper initialization and lifecycle management.

        Args:
            context: The context to use for messaging
            service_class: The service class to instantiate (defaults to cls)
            options: Optional service configuration
            post_init: Optional callback to run after initialization

        Returns:
            The started service instance

        Example:
            async def main():
                ctx = Context()
                await ctx.init("amqp://localhost")
                await CalculatorService.start(ctx, CalculatorService)
        """
        svc_class = service_class or cls
        service = svc_class(context, options)

        # Parse the proto file
        try:
            context.factory.parse(service.Proto, service.service_name)
        except Exception:
            # Proto may not exist or be empty, which is fine for some services
            context.factory.parse("", service.service_name)

        # Initialize the service
        await service.init()

        # Run post-init callback if provided
        if post_init:
            result = post_init(service)
            if asyncio.iscoroutine(result):
                await result

        Logger.info(f"Service {service.service_name} started")

        # Run the service (blocks until shutdown)
        await service.run()

        return service
