"""
Cooperative cancellation, in the shape of the DOM ``AbortController`` /
``AbortSignal`` pair the TypeScript port hands to handlers.

A service method receives a ``AbortSignal`` as part of its
``MessageHandlerContext``; it aborts when the processing timeout elapses or
when a streaming caller cancels. A streaming *caller* can pass its own
signal in ``StreamOptions`` to cancel from anywhere — a Stop button, an HTTP
request going away, a deadline.

Python idiom is preserved: ``signal.aborted`` is a property, ``await
signal.wait()`` parks until abort, ``async with``-style cleanup is left to
the caller, and ``signal.add_listener(cb)`` registers a plain callable.
"""

import asyncio
from typing import Any, Callable, List, Optional


class AbortSignal:
    """Read-only side: observe cancellation, never trigger it."""

    def __init__(self) -> None:
        self._aborted = False
        self._reason: Any = None
        self._listeners: List[Callable[[], Any]] = []
        self._event: Optional[asyncio.Event] = None

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def reason(self) -> Any:
        return self._reason

    def add_listener(self, callback: Callable[[], Any]) -> None:
        """Call ``callback`` on abort, or at once if already aborted."""
        if self._aborted:
            callback()
            return
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], Any]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    # DOM parity aliases.
    def add_event_listener(self, _event: str, callback: Callable[[], Any], *_args: Any, **_kwargs: Any) -> None:
        self.add_listener(callback)

    def remove_event_listener(self, _event: str, callback: Callable[[], Any]) -> None:
        self.remove_listener(callback)

    def _get_event(self) -> asyncio.Event:
        if self._event is None:
            self._event = asyncio.Event()
            if self._aborted:
                self._event.set()
        return self._event

    async def wait(self) -> None:
        """Park until aborted. Returns immediately if already aborted."""
        await self._get_event().wait()

    def throw_if_aborted(self) -> None:
        if self._aborted:
            raise asyncio.CancelledError(self._reason if self._reason is not None else "aborted")

    def _abort(self, reason: Any = None) -> None:
        if self._aborted:
            return
        self._aborted = True
        self._reason = reason
        if self._event is not None:
            self._event.set()
        listeners = self._listeners
        self._listeners = []
        for callback in listeners:
            try:
                callback()
            except Exception:
                # A listener that throws must not stop the others from hearing.
                pass


class AbortController:
    """Owns an AbortSignal and is the only thing that can abort it."""

    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: Any = None) -> None:
        self.signal._abort(reason)
