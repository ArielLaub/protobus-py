"""Service proxy for dynamic remote service method calls."""

from typing import Any, Callable, Dict, Optional

from .context import IContext
from .errors import (
    AlreadyInitializedError,
    InvalidRequestError,
    InvalidResponseError,
    InvalidServiceNameError,
    PublishMessageError,
)
from .logger import Logger


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
            for method_name, method_desc in getattr(service, "methods", {}).items():
                self._create_method(method_name)
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

        Args:
            method_name: Name of the method to create

        Returns:
            The proxy method
        """
        method_full_name = f"{self._service_name}.{method_name}"

        async def proxy_method(
            request_message: Any,
            actor: Optional[str] = None,
            rpc: bool = True,
        ) -> Any:
            """
            Call the remote service method.

            Args:
                request_message: Request data
                actor: Optional actor identifier
                rpc: Whether to wait for response (default True)

            Returns:
                Response data if rpc=True, empty dict otherwise
            """
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
                response_data = await self._context.publish_message(
                    buffer, f"REQUEST.{method_full_name}", rpc
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

            return response.result.get("data") if response.result else None

        self._methods[method_name] = proxy_method
        setattr(self, method_name, proxy_method)
        return proxy_method

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
