"""Service cluster for managing multiple message services."""

from typing import Generic, List, Type, TypeVar

from .context import IContext
from .logger import Logger
from .message_service import MessageService

T = TypeVar("T", bound=MessageService)


class ServiceCluster:
    """
    Manager for multiple message services.

    Allows registering and initializing multiple services with a shared context.
    """

    def __init__(self, context: IContext):
        """
        Initialize the service cluster.

        Args:
            context: The shared context for all services
        """
        self._context = context
        self._services: List[MessageService] = []

    def use(self, service_class: Type[T], count: int = 1) -> T:
        """
        Register a service class with the cluster.

        Args:
            service_class: The service class to instantiate
            count: Number of instances to create (for load balancing)

        Returns:
            The first service instance (for method access)
        """
        service = service_class(self._context)

        # Parse the proto for this service
        self._context.factory.parse(service.Proto, service.service_name)

        for i in range(count):
            self._services.append(service)
            if i < count - 1:
                service = service_class(self._context)

        return service

    async def init(self) -> None:
        """Initialize all registered services."""
        for service in self._services:
            Logger.info(f"Initializing service class {service.service_name}")
            await service.init()

    @property
    def service_names(self) -> List[str]:
        """Get the names of all registered services."""
        return [s.service_name for s in self._services]

    # TypeScript API compatibility
    @property
    def ServiceNames(self) -> List[str]:
        """Get the names of all registered services (TypeScript API)."""
        return self.service_names
