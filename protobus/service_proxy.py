"""Service proxy for dynamic remote service method calls."""

from typing import Any, AsyncIterator, Callable, Dict, Optional

from .context import IContext
from .errors import (
    AlreadyInitializedError,
    InvalidRequestError,
    InvalidResponseError,
    InvalidServiceNameError,
    PublishMessageError,
)
from .logger import Logger
from .priority import validate_message_priority


class ServiceProxy:
    """
    Dynamic wrapper for remote service method calls.

    Automatically creates methods based on the service definition,
    allowing for type-safe RPC calls.
    """

    def __init__(self, context: IContext, service_name: str):
        """
        Initialize the service proxy.

        Args:
            context: The context to use for messaging
            service_name: Fully qualified service name (e.g., "package.ServiceName")
        """
        self._context = context
        self._service_name = service_name
        self._is_initialized = False
        self._methods: Dict[str, Callable[..., Any]] = {}

    @property
    def service_name(self) -> str:
        """Get the service name."""
        return self._service_name

    async def init(self) -> None:
        """
        Initialize the service proxy.

        Creates dynamic methods based on the service definition.
        """
        if self._is_initialized:
            Logger.error(f"Already initialized service proxy {self._service_name}")
            raise AlreadyInitializedError()

        # In the TypeScript version, this looks up the service definition
        # from the protobuf root and creates methods dynamically.
        # For Python, we'll create a generic call mechanism.

        # Check if service exists in factory
        root = self._context.factory.root
        service = root.lookup_service(self._service_name)

        # If service definition is found, create typed methods
        if service:
            # Create methods from service definition
            # methods can be a dict (older protobuf) or a list-like sequence (newer protobuf)
            methods = getattr(service, "methods", [])
            if hasattr(methods, "items"):
                for method_name, method_desc in methods.items():
                    self._create_method(method_name)
            else:
                for method_desc in methods:
                    self._create_method(method_desc.name)
        else:
            # Service not found in proto definitions
            # This is okay - methods will be created on demand via __getattr__
            Logger.debug(
                f"Service {self._service_name} not found in proto definitions, "
                "using dynamic method creation"
            )

        self._is_initialized = True

    def _create_method(self, method_name: str) -> Callable[..., Any]:
        """
        Create a proxy method for a service method.

        Branches at build time on whether the method is declared as
        server-streaming in its .proto (the gRPC `stream` keyword on the
        response type). Streaming methods return an AsyncIterator; unary
        methods return a coroutine that awaits a single response.

        Args:
            method_name: Name of the method to create

        Returns:
            The proxy method
        """
        method_full_name = f"{self._service_name}.{method_name}"

        if self._context.factory.is_streaming_method(method_full_name):
            return self._create_streaming_method(method_name)

        async def proxy_method(
            request_message: Any,
            actor: Optional[str] = None,
            rpc: bool = True,
            *,
            priority: Optional[int] = None,
        ) -> Any:
            """
            Call the remote service method.

            Args:
                request_message: Request data
                actor: Optional actor identifier
                rpc: Whether to wait for response (default True)
                priority: Optional AMQP message priority (0..255), keyword-only.

                    Keyword-only on purpose: no existing call site can bind a
                    4th positional argument to it by accident, so every caller
                    written before this parameter existed keeps its exact
                    meaning.

                    Only affects ordering when the target service declared its
                    queue with ``x-max-priority`` (MessageServiceOptions
                    ``max_priority``). Against a service that did not, the
                    broker accepts the property and ignores it — which is what
                    makes a new client safe to deploy against an old service.

            Returns:
                Response data if rpc=True, empty dict otherwise
            """
            # Validated here, outside the try below, so an invalid priority
            # surfaces as InvalidPriorityError rather than being flattened
            # into a PublishMessageError about a dispatch that never happened.
            priority = validate_message_priority(priority)

            try:
                buffer = self._context.factory.build_request(
                    method_full_name, request_message, actor
                )
            except Exception as error:
                Logger.error(
                    f"Failed building message for {method_full_name} "
                    f"from {request_message}\n{error}"
                )
                raise InvalidRequestError("Failed parsing message")

            try:
                # `priority` is passed only when one was actually asked for.
                # IContext is a Protocol, so a caller may be supplying their
                # own context object written before this parameter existed;
                # the default path must stay call-compatible with those.
                if priority is None:
                    response_data = await self._context.publish_message(
                        buffer, f"REQUEST.{method_full_name}", rpc
                    )
                else:
                    response_data = await self._context.publish_message(
                        buffer, f"REQUEST.{method_full_name}", rpc, priority=priority
                    )
            except Exception as error:
                Logger.error(str(error))
                raise PublishMessageError(
                    f"Failed dispatching request to {method_full_name}"
                )

            if rpc is False:
                Logger.debug("Received non-rpc result, sending back empty answer")
                return {}

            try:
                response = self._context.factory.decode_response(response_data)
                Logger.debug(f"Received result for message {method_full_name}")
            except Exception as error:
                Logger.error(str(error))
                raise InvalidResponseError(
                    f"Failed parsing result for {method_full_name}"
                )

            if response.error:
                err = Exception(response.error.get("message", "Unknown error"))
                if response.error.get("code"):
                    setattr(err, "code", response.error["code"])
                raise err

            if response.result is None:
                return None
            # Binary protobuf decode returns the inner message directly;
            # legacy JSON format wraps in {"data": ...}
            if isinstance(response.result, dict) and "data" in response.result and len(response.result) <= 2:
                return response.result["data"]
            return response.result

        self._methods[method_name] = proxy_method
        setattr(self, method_name, proxy_method)
        return proxy_method

    def _create_streaming_method(self, method_name: str) -> Callable[..., Any]:
        """
        Create a proxy method that returns an AsyncIterator of decoded chunks.

        The returned function is an async-generator function — callers iterate
        it with ``async for``. Internally it publishes one request and drains
        the streaming reply queue, decoding each chunk's ResponseContainer.
        A terminal chunk carrying an error raises out of the iteration.
        """
        method_full_name = f"{self._service_name}.{method_name}"

        async def streaming_proxy_method(
            request_message: Any,
            actor: Optional[str] = None,
            *,
            stream_idle_timeout_ms: Optional[int] = None,
        ) -> AsyncIterator[Any]:
            try:
                buffer = self._context.factory.build_request(
                    method_full_name, request_message, actor
                )
            except Exception as error:
                Logger.error(
                    f"Failed building streaming request for {method_full_name} "
                    f"from {request_message}\n{error}"
                )
                raise InvalidRequestError("Failed parsing message")

            chunk_iter = self._context.publish_streaming_message(
                buffer,
                f"REQUEST.{method_full_name}",
                stream_idle_timeout_ms=stream_idle_timeout_ms,
            )

            try:
                async for response_data in chunk_iter:
                    try:
                        response = self._context.factory.decode_response(response_data)
                    except Exception as error:
                        Logger.error(str(error))
                        raise InvalidResponseError(
                            f"Failed parsing streaming chunk for {method_full_name}"
                        )

                    # Errors arrive as terminal chunks with response.error set.
                    if response.error:
                        err = Exception(response.error.get("message", "Unknown error"))
                        if response.error.get("code"):
                            setattr(err, "code", response.error["code"])
                        raise err

                    # Same unwrapping rule as the unary path: legacy JSON shape
                    # wraps in {"data": ...}; protobuf decode returns the inner
                    # message directly.
                    result = response.result
                    if result is None:
                        continue
                    if (
                        isinstance(result, dict)
                        and "data" in result
                        and len(result) <= 2
                    ):
                        yield result["data"]
                    else:
                        yield result
            finally:
                # If the caller breaks out, this closes the underlying
                # dispatcher iterator and releases the pending-streams slot.
                aclose = getattr(chunk_iter, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:
                        pass

        self._methods[method_name] = streaming_proxy_method
        setattr(self, method_name, streaming_proxy_method)
        return streaming_proxy_method

    def __getattr__(self, name: str) -> Any:
        """
        Dynamic method access for service methods.

        Creates methods on-demand if not already created.
        """
        # Avoid recursion for private attributes
        if name.startswith("_"):
            raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

        # Check if method already exists
        if name in self._methods:
            return self._methods[name]

        # Create method dynamically
        if self._is_initialized:
            return self._create_method(name)

        raise AttributeError(
            f"ServiceProxy not initialized. Call init() first."
        )
