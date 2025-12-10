# ServiceProxy API

The `ServiceProxy` class provides a dynamic client for calling remote services. It automatically creates methods based on the service definition, enabling RPC calls.

## Import

```python
from protobus import ServiceProxy
```

## Constructor

```python
ServiceProxy(context: IContext, service_name: str)
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `context` | `IContext` | The context for messaging |
| `service_name` | `str` | Fully qualified service name |

## Properties

### service_name

```python
@property
def service_name(self) -> str
```

Returns the service name this proxy connects to.

## Methods

### init

```python
async def init(self) -> None
```

Initialize the proxy. Must be called before making RPC calls.

**Raises:** `AlreadyInitializedError` if called twice.

**Example:**

```python
proxy = ServiceProxy(ctx, "calculator.MathService")
await proxy.init()
```

### Dynamic Methods

After initialization, methods are created dynamically based on usage:

```python
result = await proxy.method_name(request_data, actor=None, rpc=True)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `request_data` | `Any` | Request data (JSON-serializable dict) |
| `actor` | `str` | Optional caller identifier |
| `rpc` | `bool` | Wait for response (default: True) |

**Returns:** Response data (dict) if `rpc=True`, empty dict otherwise.

## Example: Basic Usage

```python
from protobus import Context, ServiceProxy

async def main():
    ctx = Context()
    await ctx.init("amqp://guest:guest@localhost:5672/")

    # Create proxy
    calc = ServiceProxy(ctx, "calculator.MathService")
    await calc.init()

    # Make RPC calls - methods are created dynamically
    result = await calc.add({"a": 5, "b": 3})
    print(f"Result: {result['result']}")  # 8

    result = await calc.multiply({"a": 4, "b": 7})
    print(f"Result: {result['result']}")  # 28

    await ctx.close()
```

## Example: With Actor

Pass an actor identifier for tracing/audit:

```python
result = await proxy.process_order(
    {"order_id": "12345"},
    actor="user-john-doe"
)
```

## Example: Fire-and-Forget

Set `rpc=False` to not wait for response:

```python
# Send notification without waiting
await proxy.send_notification(
    {"message": "Hello!"},
    rpc=False
)
# Returns immediately with {}
```

## Example: Type-Safe Wrapper

For better IDE support and type checking, create a wrapper class:

```python
from typing import TypedDict
from protobus import Context, ServiceProxy

class AddRequest(TypedDict):
    a: int
    b: int

class AddResponse(TypedDict):
    result: int

class MultiplyRequest(TypedDict):
    a: int
    b: int

class MultiplyResponse(TypedDict):
    result: int

class CalculatorProxy:
    """Type-safe wrapper for calculator service."""

    def __init__(self, proxy: ServiceProxy):
        self._proxy = proxy

    async def add(self, request: AddRequest) -> AddResponse:
        return await self._proxy.add(request)

    async def multiply(self, request: MultiplyRequest) -> MultiplyResponse:
        return await self._proxy.multiply(request)


# Usage
async def main():
    ctx = Context()
    await ctx.init("amqp://...")

    proxy = ServiceProxy(ctx, "calculator.MathService")
    await proxy.init()

    calc = CalculatorProxy(proxy)

    # Now with type hints!
    result = await calc.add({"a": 5, "b": 3})  # IDE knows this returns AddResponse
    print(result["result"])  # IDE knows this is int
```

## Error Handling

### Service Errors

Errors from the service are raised as exceptions:

```python
try:
    result = await proxy.divide({"a": 10, "b": 0})
except Exception as e:
    print(f"Error: {e}")  # "Cannot divide by zero"
    if hasattr(e, "code"):
        print(f"Code: {e.code}")  # "DIVISION_ERROR"
```

### Connection Errors

```python
from protobus import DisconnectedError, PublishMessageError

try:
    result = await proxy.some_method(data)
except DisconnectedError:
    print("Connection lost during RPC")
except PublishMessageError:
    print("Failed to send request")
except asyncio.TimeoutError:
    print("Request timed out")
```

### Validation Errors

```python
from protobus import InvalidRequestError, InvalidResponseError

try:
    result = await proxy.method(data)
except InvalidRequestError:
    print("Failed to encode request")
except InvalidResponseError:
    print("Failed to decode response")
```

## Timeout Configuration

RPC timeout is configured via environment variable:

```bash
export MESSAGE_PROCESSING_TIMEOUT=30000  # 30 seconds
```

Default is 600000ms (10 minutes).

## Multiple Proxies

Create separate proxies for different services:

```python
# Calculator service
calc = ServiceProxy(ctx, "calculator.MathService")
await calc.init()

# User service
users = ServiceProxy(ctx, "users.UserService")
await users.init()

# Order service
orders = ServiceProxy(ctx, "orders.OrderService")
await orders.init()

# Use them
result = await calc.add({"a": 1, "b": 2})
user = await users.get_user({"id": "123"})
order = await orders.create_order({"user_id": user["id"], "total": result["result"]})
```

---

See also: [MessageService](message-service.md) | [Context](context.md)
