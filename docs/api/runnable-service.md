# RunnableService

`RunnableService` extends `MessageService` with lifecycle management capabilities for production microservices.

## Overview

```python
from protobus import RunnableService

class MyService(RunnableService):
    @property
    def service_name(self) -> str:
        return "my.Service"

    async def my_method(self, data: dict, actor: str, correlation_id: str) -> dict:
        return {"result": "success"}
```

## Features

- **Automatic proto filename resolution**: Derives proto filename from service name
- **Graceful shutdown handling**: Responds to SIGINT and SIGTERM signals
- **Static bootstrap method**: Easy service startup with `start()`
- **Cleanup hook**: Override `cleanup()` for custom shutdown logic

## Class Definition

```python
class RunnableService(MessageService):
    """Abstract base class for services with lifecycle management."""

    @property
    @abstractmethod
    def service_name(self) -> str:
        """Get the service name."""
        ...

    @property
    def proto_file_name(self) -> str:
        """Get the proto file name (auto-derived from service_name)."""
        ...

    async def cleanup(self) -> None:
        """Clean up resources on shutdown. Override for custom logic."""
        pass

    async def run(self) -> None:
        """Run the service until shutdown signal is received."""
        ...

    @classmethod
    async def start(
        cls,
        context: IContext,
        service_class: Optional[Type[T]] = None,
        options: Optional[MessageServiceOptions] = None,
        post_init: Optional[Callable[[T], Any]] = None,
    ) -> T:
        """Bootstrap and start a service."""
        ...
```

## Proto Filename Convention

`RunnableService` automatically derives the proto filename from the service name:

| Service Name | Proto Filename |
|-------------|----------------|
| `Calculator.Service` | `Calculator.proto` |
| `combat.Player` | `combat.proto` |
| `my.package.MyService` | `my.proto` |

Override `proto_file_name` property if you need a different convention.

## Starting a Service

### Using the `start()` method

The recommended way to start a service:

```python
import asyncio
from protobus import Context

async def main():
    ctx = Context()
    await ctx.init("amqp://localhost")

    # Start with lifecycle management
    await MyService.start(ctx, MyService)

asyncio.run(main())
```

### With post-initialization callback

```python
async def setup_database(service):
    service.db = await Database.connect()

await MyService.start(
    ctx,
    MyService,
    post_init=setup_database
)
```

### With custom options

```python
from protobus import MessageServiceOptions, RetryOptions

options = MessageServiceOptions(
    max_concurrent=10,
    retry=RetryOptions(max_retries=5)
)

await MyService.start(ctx, MyService, options=options)
```

## Custom Cleanup

Override `cleanup()` to handle graceful shutdown:

```python
class DatabaseService(RunnableService):
    def __init__(self, context):
        super().__init__(context)
        self.db = None

    async def init(self):
        await super().init()
        self.db = await Database.connect()

    async def cleanup(self):
        """Close database connections on shutdown."""
        if self.db:
            await self.db.close()
            print("Database connection closed")

    @property
    def service_name(self) -> str:
        return "database.Service"
```

## Signal Handling

`RunnableService` automatically handles:
- **SIGINT** (Ctrl+C)
- **SIGTERM** (container orchestration)

When a signal is received:
1. The service logs the shutdown initiation
2. `cleanup()` is called
3. The service stops gracefully

## Example: Complete Service

```python
import asyncio
from protobus import RunnableService, Context, HandledError


class CalculatorService(RunnableService):
    """Calculator microservice with lifecycle management."""

    @property
    def service_name(self) -> str:
        return "calculator.MathService"

    async def add(self, data: dict, actor: str, correlation_id: str) -> dict:
        a = data.get("a", 0)
        b = data.get("b", 0)
        return {"result": a + b}

    async def divide(self, data: dict, actor: str, correlation_id: str) -> dict:
        a = data.get("a", 0)
        b = data.get("b", 0)
        if b == 0:
            raise HandledError("Division by zero", code="DIVISION_BY_ZERO")
        return {"result": a / b}


async def main():
    ctx = Context()
    await ctx.init("amqp://guest:guest@localhost:5672/")

    print("Starting calculator service...")
    await CalculatorService.start(ctx, CalculatorService)


if __name__ == "__main__":
    asyncio.run(main())
```

## Comparison with MessageService

| Feature | MessageService | RunnableService |
|---------|---------------|-----------------|
| RPC methods | ✅ | ✅ |
| Event handling | ✅ | ✅ |
| Proto filename | Manual | Auto-derived |
| Signal handling | Manual | Automatic |
| Cleanup hook | No | Yes |
| Bootstrap method | No | `start()` |

Use `RunnableService` for standalone microservices. Use `MessageService` directly when you need custom lifecycle management or are embedding services in a larger application.
