# MessageService API

The `MessageService` class is the base class for implementing RPC services. Extend this class to create services that handle requests and publish events.

## Import

```python
from protobus import MessageService, MessageServiceOptions, RetryOptions
```

## Abstract Class

```python
class MessageService(ABC):
    def __init__(
        self,
        context: IContext,
        options: Optional[MessageServiceOptions] = None
    )
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `context` | `IContext` | The context for messaging |
| `options` | `MessageServiceOptions` | Optional service configuration |

### MessageServiceOptions

```python
class MessageServiceOptions:
    max_concurrent: Optional[int] = None
    retry: Optional[RetryOptions] = None
```

### RetryOptions

```python
@dataclass
class RetryOptions:
    max_retries: int = 3
    retry_delay_ms: int = 5000
    message_ttl_ms: Optional[int] = None
```

| Option | Default | Description |
|--------|---------|-------------|
| `max_retries` | 3 | Max retry attempts (0 = no retry) |
| `retry_delay_ms` | 5000 | Delay between retries (ms) |
| `message_ttl_ms` | None | Message expiration (None = no expiry) |

## Abstract Properties

You must implement these properties in your subclass:

### service_name

```python
@property
@abstractmethod
def service_name(self) -> str
```

The fully qualified service name (e.g., `"package.ServiceName"`).

### proto_file_name

```python
@property
@abstractmethod
def proto_file_name(self) -> str
```

Path to the proto file (or empty string if not using proto files).

## Properties

### Proto

```python
@property
def Proto(self) -> str
```

Returns the proto file content. Raises `MissingProtoError` if file doesn't exist.

### player_id, name, etc.

Services can define additional properties as needed for their domain.

## Methods

### init

```python
async def init(self) -> None
```

Initialize the service. Sets up message listener, event listener, and subscribes to requests.

**Example:**

```python
service = MyService(ctx)
await service.init()
```

### publish_event

```python
async def publish_event(
    self,
    event_type: str,
    data: Any,
    topic: Optional[str] = None
) -> None
```

Publish an event from this service.

| Parameter | Type | Description |
|-----------|------|-------------|
| `event_type` | `str` | Event type name |
| `data` | `Any` | Event data (JSON-serializable) |
| `topic` | `str` | Optional custom topic |

**Example:**

```python
await self.publish_event("OrderCreated", {"order_id": "123"})
```

### subscribe_event

```python
async def subscribe_event(
    self,
    event_type: str,
    handler: EventHandler,
    topic: Optional[str] = None
) -> None
```

Subscribe to events.

| Parameter | Type | Description |
|-----------|------|-------------|
| `event_type` | `str` | Event type to subscribe to |
| `handler` | `EventHandler` | Async handler function |
| `topic` | `str` | Optional custom topic pattern |

**EventHandler signature:**

```python
async def handler(data: Any, topic: str) -> None
```

**Example:**

```python
async def on_order_created(self, data, topic):
    print(f"Order {data['order_id']} created")

await self.subscribe_event("OrderCreated", self.on_order_created)
```

## Implementing RPC Methods

RPC methods are defined as async methods on your service class. They receive:

| Parameter | Type | Description |
|-----------|------|-------------|
| `data` | `dict` | Request data |
| `actor` | `str` | Optional caller identifier |
| `correlation_id` | `str` | Unique request ID |

Methods must return a dict (JSON-serializable).

**Example:**

```python
async def add(self, data: dict, actor: str, correlation_id: str) -> dict:
    a = data.get("a", 0)
    b = data.get("b", 0)
    return {"result": a + b}
```

## Example: Complete Service

```python
from protobus import (
    Context,
    MessageService,
    MessageServiceOptions,
    RetryOptions,
    HandledError
)

class CalculatorService(MessageService):
    @property
    def service_name(self) -> str:
        return "calculator.MathService"

    @property
    def proto_file_name(self) -> str:
        return "calculator.proto"

    async def init(self):
        await super().init()
        # Subscribe to events after base init
        await self.subscribe_event("ResetRequested", self.on_reset)

    async def add(self, data: dict, actor: str, correlation_id: str) -> dict:
        """Add two numbers."""
        a = data.get("a", 0)
        b = data.get("b", 0)
        result = a + b

        # Publish event
        await self.publish_event("CalculationDone", {
            "operation": "add",
            "result": result
        })

        return {"result": result}

    async def divide(self, data: dict, actor: str, correlation_id: str) -> dict:
        """Divide two numbers."""
        a = data.get("a", 0)
        b = data.get("b", 0)

        if b == 0:
            # HandledError won't trigger retry
            raise HandledError("Cannot divide by zero", code="DIVISION_ERROR")

        return {"result": a / b}

    async def on_reset(self, data: Any, topic: str) -> None:
        """Handle reset events."""
        print("Calculator reset requested")


# Usage
async def main():
    ctx = Context()
    await ctx.init("amqp://guest:guest@localhost:5672/")

    options = MessageServiceOptions(
        max_concurrent=10,
        retry=RetryOptions(max_retries=3, retry_delay_ms=5000)
    )

    service = CalculatorService(ctx, options)
    ctx.factory.parse("", service.service_name)
    await service.init()

    print(f"Service {service.service_name} running...")
```

## Error Handling

### HandledError

Use `HandledError` for expected errors that shouldn't trigger retries:

```python
from protobus import HandledError

async def my_method(self, data, actor, correlation_id):
    if not data.get("required_field"):
        raise HandledError("Missing required field", code="VALIDATION_ERROR")

    # ... process ...
```

### Regular Exceptions

Regular exceptions will:
1. Be caught and returned as error response
2. Trigger retry if retries are enabled
3. Go to DLQ after max retries

```python
async def my_method(self, data, actor, correlation_id):
    # This will trigger retry
    result = await some_external_api()  # May raise
    return {"data": result}
```

---

See also: [ServiceProxy](service-proxy.md) | [Events](events.md) | [Error Handling](../advanced/error-handling.md)
