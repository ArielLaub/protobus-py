"""Proxied service - message service with built-in service proxy."""

from abc import abstractmethod
from typing import Generic, Optional, TypeVar

from .context import IContext
from .message_service import MessageService, MessageServiceOptions
from .service_proxy import ServiceProxy

T = TypeVar("T")


class ProxiedService(MessageService, Generic[T]):
    """
    Abstract message service with a strongly typed internal proxy.

    This allows services to call their own methods through the message bus,
    useful for self-calls that need to go through the standard routing.

    Note: Due to Python's type system limitations, child classes must declare
    the interface both as a type parameter and potentially implement it.
    """

    def __init__(
        self,
        context: IContext,
        options: Optional[MessageServiceOptions] = None,
    ):
        """
        Initialize the proxied service.

        Args:
            context: The context to use for messaging
            options: Optional service configuration
        """
        super().__init__(context, options)
        self._proxy: Optional[T] = None

    @property
    def proxy(self) -> T:
        """
        Get the service proxy.

        Returns:
            The typed service proxy

        Raises:
            RuntimeError: If init() hasn't been called
        """
        if self._proxy is None:
            raise RuntimeError("ProxiedService not initialized. Call init() first.")
        return self._proxy

    async def init(self) -> None:
        """Initialize the service and its proxy."""
        # Initialize parent class
        await super().init()

        # Create and initialize the proxy
        service_proxy = ServiceProxy(self._context, self.service_name)
        await service_proxy.init()

        # Cast to the generic type
        # In Python, this is a type hint; runtime behavior is duck-typed
        self._proxy = service_proxy  # type: ignore
