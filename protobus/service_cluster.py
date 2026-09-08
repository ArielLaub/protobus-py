"""ServiceCluster: several services on one Context, initialised together.
A Python-port extra; the TypeScript port has no equivalent."""

from typing import List, Type, TypeVar

from .context import IContext
from .logger import Logger
from .message_service import MessageService

T = TypeVar("T", bound=MessageService)


class ServiceCluster:
    def __init__(self, context: IContext) -> None:
        self._context = context
        self._services: List[MessageService] = []

    def use(self, service_class: Type[T], count: int = 1) -> T:
        """Register ``count`` instances of a service class. Returns the first."""
        first = service_class(self._context)
        self._services.append(first)
        for _ in range(count - 1):
            self._services.append(service_class(self._context))
        return first

    async def init(self) -> None:
        for service in self._services:
            Logger.info(f"initializing service {service.service_name}")
            await service.init()

    async def stop_consuming(self) -> None:
        for service in self._services:
            await service.stop_consuming()

    async def close(self) -> None:
        for service in self._services:
            await service.close()

    @property
    def services(self) -> List[MessageService]:
        return list(self._services)

    @property
    def service_names(self) -> List[str]:
        return [s.service_name for s in self._services]
