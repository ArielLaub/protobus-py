"""A minimal EventEmitter, so components subscribe to a connection the way
they do in the TypeScript port: ``connection.on('reconnected', cb)``."""

import asyncio
from typing import Any, Callable, Dict, List, Set

from .logger import Logger


class EventEmitter:
    """
    Synchronous emit with async-tolerant handlers.

    A handler that returns a coroutine is scheduled as a task and kept
    referenced until it finishes: asyncio holds only a weak reference to a
    running task, so an unreferenced one can be collected mid-await.
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Callable[..., Any]]] = {}
        self._once: Set[Callable[..., Any]] = set()
        self._pending: Set["asyncio.Task[Any]"] = set()

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        self._handlers.setdefault(event, []).append(callback)

    def once(self, event: str, callback: Callable[..., Any]) -> None:
        self._once.add(callback)
        self.on(event, callback)

    def off(self, event: str, callback: Callable[..., Any]) -> None:
        handlers = self._handlers.get(event)
        if not handlers:
            return
        try:
            handlers.remove(callback)
        except ValueError:
            return
        self._once.discard(callback)
        if not handlers:
            del self._handlers[event]

    # TS parity.
    remove_listener = off
    add_listener = on

    def remove_all_listeners(self, event: str = "") -> None:
        if event:
            self._handlers.pop(event, None)
        else:
            self._handlers.clear()

    def listener_count(self, event: str) -> int:
        return len(self._handlers.get(event, []))

    def emit(self, event: str, *args: Any) -> bool:
        # Copy: a handler may unsubscribe itself (or another) during emission.
        handlers = list(self._handlers.get(event, []))
        for handler in handlers:
            if handler in self._once:
                self.off(event, handler)
            try:
                result = handler(*args)
            except Exception as err:
                Logger.error(f"error in '{event}' handler: {err}")
                continue
            if asyncio.iscoroutine(result):
                try:
                    task = asyncio.get_running_loop().create_task(result)
                except RuntimeError:
                    result.close()
                    continue
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
        return bool(handlers)

    @property
    def pending_handler_tasks(self) -> Set["asyncio.Task[Any]"]:
        """Async handlers still running; awaited by tests and by shutdown."""
        return self._pending
