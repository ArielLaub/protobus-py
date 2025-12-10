# Error Handling

This guide covers error handling strategies in Protobus, including retries, dead-letter queues, and the `HandledError` pattern.

## Error Types

### HandledError

Use `HandledError` for expected errors that should **not** trigger retries:

```python
from protobus import HandledError

class ValidationError(HandledError):
    """Validation errors don't need retry."""
    pass

class NotFoundError(HandledError):
    """Resource not found - retry won't help."""
    pass

class PermissionError(HandledError):
    """Permission denied - retry won't help."""
    pass
```

### Usage in Services

```python
from protobus import MessageService, HandledError

class UserService(MessageService):
    async def get_user(self, data, actor, correlation_id):
        user_id = data.get("id")

        if not user_id:
            # HandledError - won't retry
            raise HandledError("User ID is required", code="VALIDATION_ERROR")

        user = await self._db.find_user(user_id)
        if not user:
            # HandledError - won't retry
            raise HandledError(f"User {user_id} not found", code="NOT_FOUND")

        return user

    async def update_user(self, data, actor, correlation_id):
        try:
            # This might fail temporarily (network, db connection)
            # Regular exceptions WILL be retried
            return await self._db.update(data)
        except DatabaseConnectionError:
            # Re-raise to trigger retry
            raise
```

### Checking Error Type

```python
from protobus import is_handled_error

try:
    result = await service.method(data)
except Exception as e:
    if is_handled_error(e):
        print(f"Business error: {e}")
    else:
        print(f"Infrastructure error: {e}")
```

## Retry Configuration

### Service-Level Configuration

```python
from protobus import MessageService, MessageServiceOptions, RetryOptions

options = MessageServiceOptions(
    retry=RetryOptions(
        max_retries=3,        # Retry up to 3 times
        retry_delay_ms=5000,  # 5 second delay between retries
        message_ttl_ms=60000  # Messages expire after 60 seconds
    )
)

service = MyService(ctx, options)
```

### Disabling Retries

```python
options = MessageServiceOptions(
    retry=RetryOptions(max_retries=0)  # No retries
)
```

## Retry Flow

```
Message Processing
        │
        ▼
   ┌─────────┐
   │ Handler │
   └────┬────┘
        │
   ┌────┴────┐
   │         │
Success    Error
   │         │
   ▼         ▼
  ACK    ┌─────────────┐
         │ HandledError?│
         └──────┬──────┘
           ┌────┴────┐
           │         │
          Yes        No
           │         │
           ▼         ▼
          ACK    ┌───────────┐
                 │ Retries   │
                 │ remaining?│
                 └─────┬─────┘
                  ┌────┴────┐
                  │         │
                 Yes        No
                  │         │
                  ▼         ▼
             Retry Queue   DLQ
```

## Dead Letter Queue (DLQ)

Messages that fail all retries are sent to a DLQ:

- Queue name: `<service_name>.DLQ`
- Example: `calculator.MathService.DLQ`

### DLQ Headers

Failed messages include headers with failure context:

| Header | Description |
|--------|-------------|
| `x-death-reason` | Error message |
| `x-death-time` | Timestamp when sent to DLQ |
| `x-retry-count` | Number of retries attempted |
| `x-first-failure-time` | When first failure occurred |
| `x-last-error` | Last error message |

### Processing DLQ Messages

Messages in DLQ can be:
1. Manually inspected in RabbitMQ Management UI
2. Reprocessed after fixing the issue
3. Analyzed for patterns and bugs

```python
# Example: DLQ processor (manual implementation)
async def process_dlq():
    # Connect to DLQ and process messages
    # This would be a separate service or script
    pass
```

## Client-Side Error Handling

### Catching Service Errors

```python
from protobus import ServiceProxy

proxy = ServiceProxy(ctx, "users.UserService")
await proxy.init()

try:
    user = await proxy.get_user({"id": "invalid"})
except Exception as e:
    print(f"Error: {e}")
    if hasattr(e, "code"):
        if e.code == "NOT_FOUND":
            print("User doesn't exist")
        elif e.code == "VALIDATION_ERROR":
            print("Invalid request")
```

### Catching Infrastructure Errors

```python
from protobus import DisconnectedError
import asyncio

try:
    result = await proxy.method(data)
except DisconnectedError:
    # Connection lost during RPC
    print("Connection lost, will retry...")
    await asyncio.sleep(1)
    # Implement retry logic
except asyncio.TimeoutError:
    # Request timed out
    print("Request timed out")
except Exception as e:
    # Other errors
    print(f"Error: {e}")
```

### Retry Wrapper

```python
async def with_retry(coro_func, max_retries=3, delay=1.0):
    """Retry a coroutine on failure."""
    last_error = None
    for attempt in range(max_retries):
        try:
            return await coro_func()
        except DisconnectedError:
            last_error = e
            if attempt < max_retries - 1:
                await asyncio.sleep(delay * (attempt + 1))
        except Exception as e:
            # Don't retry other errors
            raise
    raise last_error

# Usage
result = await with_retry(lambda: proxy.method(data))
```

## Event Error Handling

### In Event Handlers

```python
async def on_event(self, data, topic):
    try:
        await self._process(data)
    except Exception as e:
        # Log error but don't re-raise
        # This prevents message requeue
        print(f"Error processing {topic}: {e}")

        # Optionally publish to error topic
        await self.publish_event("EventProcessingFailed", {
            "original_topic": topic,
            "original_data": data,
            "error": str(e)
        })
```

### Error Events Pattern

```python
class ErrorHandler(MessageService):
    async def init(self):
        await super().init()
        # Subscribe to all error events
        await self.subscribe_event(
            "any",
            self.on_error,
            topic="EVENT.*.Failed"
        )

    async def on_error(self, data, topic):
        """Central error handling."""
        print(f"Error event: {topic}")
        # Log to monitoring system
        # Send alert
        # etc.
```

## Best Practices

### 1. Use HandledError for Business Logic

```python
# Good - clear separation
async def create_order(self, data, actor, cid):
    if not data.get("items"):
        raise HandledError("Order must have items", code="EMPTY_ORDER")

    if data["total"] < 0:
        raise HandledError("Invalid total", code="INVALID_TOTAL")

    return await self._create(data)  # Infrastructure errors will retry
```

### 2. Create Domain-Specific Errors

```python
class OrderError(HandledError):
    pass

class OrderNotFoundError(OrderError):
    def __init__(self, order_id):
        super().__init__(f"Order {order_id} not found", code="ORDER_NOT_FOUND")

class InsufficientInventoryError(OrderError):
    def __init__(self, item_id):
        super().__init__(f"Insufficient inventory for {item_id}", code="NO_INVENTORY")
```

### 3. Include Error Codes

```python
# Client can programmatically handle errors
raise HandledError("User not found", code="USER_NOT_FOUND")
raise HandledError("Invalid email", code="INVALID_EMAIL")
raise HandledError("Rate limited", code="RATE_LIMITED")
```

### 4. Log Before Retrying

```python
async def risky_operation(self, data, actor, cid):
    try:
        return await self._external_api(data)
    except ExternalAPIError as e:
        # Log for debugging before retry
        print(f"External API failed (will retry): {e}")
        raise  # Will trigger retry
```

---

See also: [MessageService](../api/message-service.md) | [Troubleshooting](../troubleshooting.md)
