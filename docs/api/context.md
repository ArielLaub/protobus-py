# Context API

The `Context` class is the central orchestrator for Protobus. It manages the connection to RabbitMQ, message serialization, and provides the main API for publishing messages and events.

## Import

```python
from protobus import Context, ContextOptions
```

## Constructor

```python
Context(options: Optional[ContextOptions] = None)
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `options` | `ContextOptions` | Optional configuration options |

### ContextOptions

```python
@dataclass
class ContextOptions:
    max_reconnect_attempts: int = 10
    initial_reconnect_delay_ms: int = 1000
    max_reconnect_delay_ms: int = 30000
    reconnect_backoff_multiplier: float = 2.0
```

| Option | Default | Description |
|--------|---------|-------------|
| `max_reconnect_attempts` | 10 | Maximum reconnection attempts (0 = infinite) |
| `initial_reconnect_delay_ms` | 1000 | Initial delay between retries (ms) |
| `max_reconnect_delay_ms` | 30000 | Maximum delay cap (ms) |
| `reconnect_backoff_multiplier` | 2.0 | Exponential backoff multiplier |

## Properties

### is_connected

```python
@property
def is_connected(self) -> bool
```

Returns `True` if connected to RabbitMQ.

### is_reconnecting

```python
@property
def is_reconnecting(self) -> bool
```

Returns `True` if currently attempting to reconnect.

### factory

```python
@property
def factory(self) -> MessageFactory
```

Returns the `MessageFactory` instance for message encoding/decoding.

### connection

```python
@property
def connection(self) -> IConnection
```

Returns the underlying `Connection` instance.

## Methods

### init

```python
async def init(self, url: str) -> None
```

Initialize the context and connect to RabbitMQ.

| Parameter | Type | Description |
|-----------|------|-------------|
| `url` | `str` | AMQP connection URL |

**Example:**

```python
ctx = Context()
await ctx.init("amqp://guest:guest@localhost:5672/")
```

### close

```python
async def close(self) -> None
```

Close the connection and clean up resources.

**Example:**

```python
await ctx.close()
```

### publish_message

```python
async def publish_message(
    self,
    data: bytes,
    routing_key: str,
    rpc: bool = True
) -> Optional[bytes]
```

Publish a message, optionally waiting for a response.

| Parameter | Type | Description |
|-----------|------|-------------|
| `data` | `bytes` | Encoded message data |
| `routing_key` | `str` | Message routing key |
| `rpc` | `bool` | Whether to wait for response (default: True) |

**Returns:** Response bytes if `rpc=True`, `None` otherwise.

**Note:** This is typically called internally by `ServiceProxy`. Use `ServiceProxy` for RPC calls.

### publish_event

```python
async def publish_event(
    self,
    event_type: str,
    data: Any,
    topic: Optional[str] = None
) -> None
```

Publish an event to the event exchange.

| Parameter | Type | Description |
|-----------|------|-------------|
| `event_type` | `str` | Event type name |
| `data` | `Any` | Event data (must be JSON-serializable) |
| `topic` | `str` | Optional custom topic (default: `EVENT.<event_type>`) |

**Example:**

```python
await ctx.publish_event("OrderCreated", {
    "order_id": "12345",
    "customer": "john@example.com",
    "total": 99.99
})

# With custom topic
await ctx.publish_event("OrderCreated", data, topic="EVENT.Orders.US.Created")
```

## Connection Events

Access connection events via the `connection` property:

```python
ctx.connection.on("disconnected", lambda: print("Disconnected"))
ctx.connection.on("reconnecting", lambda a, m: print(f"Reconnecting {a}/{m}"))
ctx.connection.on("reconnected", lambda: print("Reconnected"))
ctx.connection.on("error", lambda e: print(f"Error: {e}"))
```

| Event | Callback Signature | Description |
|-------|-------------------|-------------|
| `disconnected` | `() -> None` | Connection lost |
| `reconnecting` | `(attempt: int, max: int) -> None` | Reconnection in progress |
| `reconnected` | `() -> None` | Successfully reconnected |
| `error` | `(error: Exception) -> None` | Connection error |

## Example: Complete Usage

```python
import asyncio
from protobus import Context, ContextOptions

async def main():
    # Create context with custom options
    options = ContextOptions(
        max_reconnect_attempts=0,  # Infinite retries
        initial_reconnect_delay_ms=500,
        max_reconnect_delay_ms=60000
    )
    ctx = Context(options)

    # Set up event handlers
    ctx.connection.on("disconnected", lambda: print("Lost connection!"))
    ctx.connection.on("reconnected", lambda: print("Back online!"))

    # Connect
    await ctx.init("amqp://guest:guest@localhost:5672/")

    print(f"Connected: {ctx.is_connected}")

    # Publish an event
    await ctx.publish_event("AppStarted", {"version": "1.0.0"})

    # Use factory for encoding
    request = ctx.factory.build_request(
        "myservice.Method",
        {"key": "value"}
    )

    # ... use the context ...

    # Clean up
    await ctx.close()

if __name__ == "__main__":
    asyncio.run(main())
```

---

See also: [MessageService](message-service.md) | [ServiceProxy](service-proxy.md)
