"""
RunnableService: a MessageService with lifecycle management.

- Convention-based proto file name (derived from the service name)
- Graceful shutdown on SIGINT / SIGTERM: stop consuming, drain in-flight
  work, run the cleanup hook, disconnect
- ``start()`` to bootstrap a service, ``run()`` to block until shutdown
"""

import asyncio
import os
import signal
import weakref
from typing import Any, Awaitable, Callable, List, Optional, Set, Type, TypeVar

from .context import IContext
from .logger import Logger
from .message_service import MessageService, MessageServiceOptions

T = TypeVar("T", bound="RunnableService")

# Signals a running service shuts down on. Not every platform has SIGTERM.
_SHUTDOWN_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM") if hasattr(signal, name))

# Services currently inside run(), per loop, and the signals whose loop
# handler is ours. See RunnableService._install_signal_handlers.
_running_services: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Set[RunnableService]]" = weakref.WeakKeyDictionary()
_installed_signals: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, List[signal.Signals]]" = weakref.WeakKeyDictionary()


class RunnableService(MessageService):
    def __init__(self, context: IContext, options: Optional[MessageServiceOptions] = None, **kwargs: Any) -> None:
        super().__init__(context, options, **kwargs)
        self._shutdown_event: Optional[asyncio.Event] = None
        self._shutting_down = False

    @property
    def proto_file_name(self) -> str:
        """
        Convention-based proto file resolution: ``Calculator.Service`` ->
        ``Calculator.proto``, in ``PROTO_PATH`` (default ``./proto``).
        Override for a different layout.
        """
        if type(self).ProtoFileName is not MessageService.ProtoFileName:
            return self.ProtoFileName
        package_name = self.service_name.split(".")[0] or self.service_name
        return os.path.join(os.environ.get("PROTO_PATH", "./proto"), f"{package_name}.proto")

    async def cleanup(self) -> None:
        """Optional hook run during shutdown, after in-flight work drained.
        Override to close databases, flush buffers, etc."""

    # -- shutdown -------------------------------------------------------------

    async def shutdown(self, reason: str = "", exit_code: int = 0) -> None:
        """
        Graceful shutdown, in the order that keeps user resources safe:

        1. stop taking new work, keeping channels open;
        2. let work already in hand finish — including the reply, retry or
           DLQ publish that settles it — within ``SHUTDOWN_DRAIN_TIMEOUT_MS``;
        3. run ``cleanup()``;
        4. disconnect.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        Logger.info(f"Shutdown initiated{f' ({reason})' if reason else ''}")

        try:
            await self.stop_consuming()
            Logger.info("Stopped accepting new messages")
        except Exception as err:
            Logger.error(f"Failed to stop consumers: {err}")

        connection = self.context.connection
        try:
            budget = int(os.environ.get("SHUTDOWN_DRAIN_TIMEOUT_MS") or 30000)
            in_flight = getattr(connection, "in_flight_deliveries", 0)
            if in_flight > 0:
                Logger.info(f"Draining {in_flight} in-flight message(s), up to {budget}ms")
                drained = await connection.drain_in_flight(budget)
                Logger.info(
                    "In-flight messages drained" if drained else
                    f"Drain deadline reached with {connection.in_flight_deliveries} still running; "
                    "they stay unacknowledged and will be redelivered"
                )
        except Exception as err:
            Logger.error(f"Drain failed: {err}")

        try:
            await self.cleanup()
            Logger.info("Service cleanup completed")
        except Exception as err:
            Logger.error(f"Service cleanup failed: {err}")

        try:
            await connection.disconnect()
            Logger.info("Connection closed")
        except Exception as err:
            Logger.error(f"Connection close failed: {err}")

        self._exit_code = exit_code
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    _exit_code = 0

    @property
    def exit_code(self) -> int:
        return self._exit_code

    def _install_signal_handlers(self) -> Callable[[], None]:
        """
        Register this service with the loop's shutdown signals.

        ``loop.add_signal_handler`` keeps ONE handler per signal, so services
        running side by side in a process share a handler that shuts every
        registered service down; each service's ``run()`` adds itself on the
        way in and removes itself on the way out, and the loop's handlers go
        with the last one.
        """
        loop = asyncio.get_running_loop()
        running = _running_services.setdefault(loop, set())
        running.add(self)
        installed = _installed_signals.setdefault(loop, [])

        def request_shutdown(sig_name: str) -> None:
            for service in list(_running_services.get(loop, ())):
                loop.create_task(service.shutdown(f"signal: {sig_name}"))

        if not installed:
            for sig in _SHUTDOWN_SIGNALS:
                try:
                    loop.add_signal_handler(sig, request_shutdown, sig.name)
                    installed.append(sig)
                except (NotImplementedError, RuntimeError, ValueError):
                    # Windows, or not the main thread: no signal handling.
                    pass

        def remove() -> None:
            running.discard(self)
            if running:
                return
            _running_services.pop(loop, None)
            for sig in _installed_signals.pop(loop, []):
                try:
                    loop.remove_signal_handler(sig)
                except Exception:
                    pass

        return remove

    async def run(self) -> int:
        """
        Block until a shutdown signal arrives, then shut down gracefully.
        Returns the exit code (0 for a signal-initiated shutdown).
        """
        self._shutdown_event = asyncio.Event()
        remove_handlers = self._install_signal_handlers()
        Logger.info(f"Service {self.service_name} running, press Ctrl+C to stop")
        try:
            await self._shutdown_event.wait()
        finally:
            remove_handlers()
        Logger.info(f"Service {self.service_name} stopped")
        return self._exit_code

    def request_shutdown(self, reason: str = "requested") -> None:
        """Ask a running service to shut down, from anywhere in the process."""
        try:
            asyncio.get_running_loop().create_task(self.shutdown(reason))
        except RuntimeError:
            pass

    @classmethod
    async def launch(
        cls: Type[T],
        context: IContext,
        service_class: Optional[Type[T]] = None,
        options: Optional[MessageServiceOptions] = None,
        post_init: Optional[Callable[[T], Awaitable[None]]] = None,
        **option_kwargs: Any,
    ) -> T:
        """
        Instantiate and initialise a service, returning it without blocking.

        Options are passed as a MessageServiceOptions or as keywords
        (``max_concurrent=4``). A startup failure runs the shutdown sequence
        with exit code 1 and re-raises, so a supervisor sees the process fail
        rather than succeed.
        """
        service_class = service_class or cls
        if options is None and option_kwargs:
            options = MessageServiceOptions(**option_kwargs)
        elif option_kwargs:
            raise TypeError("pass either options or keyword options, not both")
        service: Optional[T] = None
        try:
            service = service_class(context, options)
            Logger.info(f"Starting service: {service.service_name}")
            await service.init()
            if post_init is not None:
                result = post_init(service)
                if asyncio.iscoroutine(result):
                    await result
            Logger.info(f"Service ready: {service.service_name}")
            return service
        except Exception as err:
            Logger.error(f"Service startup failed: {err}")
            if service is not None:
                await service.shutdown("startup failed", exit_code=1)
            else:
                try:
                    await context.connection.disconnect()
                except Exception:
                    pass
            raise

    @classmethod
    async def start(
        cls: Type[T],
        context: IContext,
        service_class: Optional[Type[T]] = None,
        options: Optional[MessageServiceOptions] = None,
        post_init: Optional[Callable[[T], Awaitable[None]]] = None,
        **option_kwargs: Any,
    ) -> T:
        """
        Bootstrap a service and run it until a shutdown signal arrives.

        The one call a service's ``main()`` needs::

            async def main():
                ctx = Context()
                await ctx.init("amqp://localhost", ["./proto"])
                await CalculatorService.start(ctx)

        Returns the service once it has shut down. Use ``launch()`` to get
        the running service back without blocking.
        """
        service = await cls.launch(context, service_class, options, post_init, **option_kwargs)
        await service.run()
        return service
