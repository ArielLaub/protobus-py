"""ServiceProxy: a typed-by-schema client for a remote service."""

import contextlib
from typing import Any, AsyncIterator, Optional

from .context import IContext
from .errors import (
    AlreadyInitializedError,
    InvalidRequestError,
    InvalidResponseError,
    InvalidServiceNameError,
)
from .logger import Logger
from .message_dispatcher import CallOptions, StreamOptions
from .message_factory import ResponseContainer


class RemoteError(Exception):
    """
    An error the remote service returned. ``code`` is whatever the service
    put on its error (``HANDLED_ERROR``, ``PROTOCOL_ERROR``, ``INTERNAL_ERROR``,
    or a custom code); ``''`` when it had none.
    """

    def __init__(self, message: str, code: str = "", method: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.method = method


class ServiceProxy:
    """
    Installs one method per rpc the contract declares. A unary method is
    ``async def method(request, actor=None, rpc=True, timeout_ms=None,
    options=None)``; a server-streaming one is ``def method(request,
    actor=None, idle_timeout_ms=None, options=None)`` returning an async
    iterator of decoded chunks.
    """

    def __init__(self, context: IContext, service_name: str) -> None:
        self._context = context
        self._service_name = service_name
        self._is_initialized = False
        # The service as the .proto declares it, which is not always the name
        # the proxy was constructed with; see _resolve_contract().
        self._contract_service_name: Optional[str] = None

    @property
    def service_name(self) -> str:
        return self._service_name

    @property
    def contract_service_name(self) -> Optional[str]:
        return self._contract_service_name

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    @property
    def context(self) -> IContext:
        return self._context

    def _resolve_contract(self) -> str:
        """
        Find the contract this proxy addresses, by trimming runtime segments
        off the name until one names a service in the root.
        """
        factory = self._context.factory
        if factory.root is None:
            raise InvalidServiceNameError(
                f"cannot resolve '{self._service_name}': the message factory has not been "
                "initialised, so no schema is loaded yet. Await Context.init() before "
                "constructing a proxy."
            )
        candidate = self._service_name
        while True:
            if factory.has_service(candidate):
                if candidate != self._service_name:
                    # Said out loud, because trimming is a guess.
                    Logger.info(
                        f"service proxy '{self._service_name}' resolved to contract '{candidate}'; "
                        f"requests will route to REQUEST.{self._service_name}.*"
                    )
                return candidate
            cut = candidate.rfind(".")
            if cut <= 0:
                raise InvalidServiceNameError(
                    f"no service in the schema matches '{self._service_name}' or any prefix of it; "
                    "the .proto must declare the service this proxy addresses"
                )
            candidate = candidate[:cut]

    async def init(self) -> None:
        if self._is_initialized:
            Logger.error(f"already initialized service proxy {self._service_name}")
            raise AlreadyInitializedError(f"service proxy {self._service_name} is already initialized")
        self._contract_service_name = self._resolve_contract()
        factory = self._context.factory
        service = factory.root.lookup_service(self._contract_service_name)  # type: ignore[union-attr]

        for method in service.methods:
            name = method.name
            # Proto method names are assigned straight onto this instance, so
            # a method called `init` or `is_initialized` would clobber the
            # proxy's own members. Fail loudly instead of half-working.
            if hasattr(self, name):
                raise InvalidServiceNameError(
                    f"proto method '{self._service_name}.{name}' collides with a ServiceProxy member; "
                    "rename it in the .proto"
                )
            # The ENVELOPE carries the contract method name, which the
            # receiving MessageService validates the body against; the
            # ROUTING KEY carries the runtime name, which reaches this
            # instance's queue.
            method_full_name = f"{self._contract_service_name}.{name}"
            routing_key = f"REQUEST.{self._service_name}.{name}"
            if factory.is_streaming_method(method_full_name):
                setattr(self, name, self._make_streaming_call(method_full_name, routing_key, method.input_type.full_name))
            else:
                setattr(self, name, self._make_unary_call(method_full_name, routing_key, method.input_type.full_name))
        self._is_initialized = True

    def _make_unary_call(self, method_full_name: str, routing_key: str, request_type: str) -> Any:
        proxy = self

        async def call(
            request_message: Any = None,
            actor: Optional[str] = None,
            rpc: bool = True,
            timeout_ms: Optional[int] = None,
            options: Optional[CallOptions] = None,
            *,
            priority: Optional[int] = None,
            message_id: Optional[str] = None,
        ) -> Any:
            if priority is not None or message_id is not None:
                options = options or CallOptions()
                if priority is not None:
                    options.priority = priority
                if message_id is not None:
                    options.message_id = message_id
            try:
                buffer = proxy._context.factory.build_request(method_full_name, request_message, actor)
            except Exception as error:
                # No payload in the log line — requests carry secrets and PII.
                Logger.error(f"failed building message '{request_type}': {error}")
                raise InvalidRequestError("failed parsing message") from error
            # The delivery error is raised as it stands: UnroutableError and
            # PublishNackedError are definite failures a caller may safely
            # retry, PublishConfirmTimeoutError and ChannelClosedError are
            # ambiguous and retrying either can duplicate.
            response_data = await proxy._context.publish_message(buffer, routing_key, rpc, timeout_ms, options)
            if rpc is False:
                Logger.debug("received non rpc result sending back empty answer")
                return {}
            return proxy._unwrap(method_full_name, response_data)

        call.__name__ = method_full_name.rsplit(".", 1)[-1]
        return call

    def _unwrap(self, method_full_name: str, response_data: Any) -> Any:
        try:
            response: ResponseContainer = self._context.factory.decode_response(response_data)
            Logger.debug(f"received result for message {method_full_name}")
        except Exception as error:
            Logger.error(f"failed parsing result for {method_full_name}: {error}")
            raise InvalidResponseError(f"failed parsing result for {method_full_name}") from error
        if response.error is not None:
            raise RemoteError(response.error.message, response.error.code, response.error.method)
        if response.result is None:
            raise InvalidResponseError(f"response for {method_full_name} carried neither a result nor an error")
        return response.result.data

    def _make_streaming_call(self, method_full_name: str, routing_key: str, request_type: str) -> Any:
        proxy = self

        def call(
            request_message: Any = None,
            actor: Optional[str] = None,
            idle_timeout_ms: Optional[int] = None,
            options: Optional[StreamOptions] = None,
        ) -> "StreamingCall":
            return StreamingCall(proxy, method_full_name, routing_key, request_type, request_message, actor, idle_timeout_ms, options)

        call.__name__ = method_full_name.rsplit(".", 1)[-1]
        return call


class StreamingCall:
    """
    The async iterator a streaming proxy method returns. Iterate it with
    ``async for``; close it early with ``await call.aclose()`` or by using it
    as ``async with``, which also tells the server to stop producing.
    """

    def __init__(
        self,
        proxy: ServiceProxy,
        method_full_name: str,
        routing_key: str,
        request_type: str,
        request_message: Any,
        actor: Optional[str],
        idle_timeout_ms: Optional[int],
        options: Optional[StreamOptions],
    ) -> None:
        self._proxy = proxy
        self._method_full_name = method_full_name
        self._chunks: Any = None
        self._build_error: Optional[BaseException] = None
        factory = proxy.context.factory
        try:
            buffer = factory.build_request(method_full_name, request_message, actor)
        except Exception as error:
            Logger.error(f"failed building streaming request '{request_type}' for {method_full_name}: {error}")
            # Surfaces inside the caller's try/except around `async for`.
            self._build_error = InvalidRequestError("failed parsing message")
            self._build_error.__cause__ = error
            return
        self._chunks = proxy.context.publish_streaming_message(buffer, routing_key, idle_timeout_ms, options)

    @property
    def correlation_id(self) -> Optional[str]:
        return getattr(self._chunks, "correlation_id", None)

    def __aiter__(self) -> "StreamingCall":
        return self

    async def __anext__(self) -> Any:
        if self._build_error is not None:
            error, self._build_error = self._build_error, None
            raise error
        if self._chunks is None:
            raise StopAsyncIteration
        while True:
            chunk = await self._chunks.__anext__()
            factory = self._proxy.context.factory
            try:
                response = factory.decode_response(chunk)
            except Exception as error:
                Logger.error(f"failed parsing streaming chunk for {self._method_full_name}: {error}")
                await self.aclose()
                raise InvalidResponseError(f"failed parsing streaming chunk for {self._method_full_name}") from error
            # Terminal chunks may carry an error instead of a result.
            if response.error is not None:
                await self.aclose()
                raise RemoteError(response.error.message, response.error.code, response.error.method)
            if response.result is not None:
                return response.result.data

    async def aclose(self) -> None:
        if self._chunks is not None:
            aclose = getattr(self._chunks, "aclose", None)
            if aclose is not None:
                await aclose()

    async def __aenter__(self) -> "StreamingCall":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()


def aclosing(stream: Any) -> Any:
    """``contextlib.aclosing`` re-exported for callers on Python 3.10+."""
    return contextlib.aclosing(stream)
