"""A MessageService with a proxy to itself, for services that call their own
contract (a cluster member reaching a sibling, for instance)."""

from typing import Any, Generic, TypeVar

from .message_service import MessageService
from .service_proxy import ServiceProxy

T = TypeVar("T")


class ProxiedService(MessageService, Generic[T]):
    _proxy: Any = None

    @property
    def proxy(self) -> T:
        if self._proxy is None:
            raise RuntimeError(f"{type(self).__name__}.proxy is available after init()")
        return self._proxy

    async def init(self) -> None:
        await super().init()
        proxy = ServiceProxy(self.context, self.service_name)
        await proxy.init()
        self._proxy = proxy
