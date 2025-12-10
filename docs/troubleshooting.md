# Troubleshooting

Common issues and their solutions when using Protobus Python.

> **Note:** This is the Python port of [Protobus](https://github.com/ArielLaub/protobus). See the [original troubleshooting docs](https://github.com/ArielLaub/protobus/blob/master/docs/troubleshooting.md) for the TypeScript version.

## Connection Issues

### Connection Refused

**Symptom:** `ConnectionRefusedError` or `AMQPConnectionError`

**Possible causes:**
1. RabbitMQ not running
2. Wrong host or port
3. Firewall blocking connection

**Solutions:**

```bash
# Check if RabbitMQ is running
docker ps | grep rabbit

# Or check system service
systemctl status rabbitmq-server

# Test connectivity
telnet localhost 5672
```

### Authentication Failed

**Symptom:** `AUTH_ERROR` or `ACCESS_REFUSED`

**Solutions:**

```python
# Verify credentials
await ctx.init("amqp://guest:guest@localhost:5672/")

# Check virtual host exists
# Default vhost is "/" - use empty string in URL or specify explicitly
await ctx.init("amqp://user:pass@localhost:5672/myvhost")
```

```bash
# List vhosts in RabbitMQ
docker exec rabbitmq rabbitmqctl list_vhosts
```

### Connection Drops

**Symptom:** Intermittent `DisconnectedError`

**Solutions:**

1. Enable reconnection (default is on):

```python
from protobus import Context, ContextOptions

options = ContextOptions(
    max_reconnect_attempts=0,  # 0 = infinite
    initial_reconnect_delay_ms=1000,
    max_reconnect_delay_ms=30000
)
ctx = Context(options)
```

2. Monitor connection events:

```python
ctx.connection.on("disconnected", lambda: print("Connection lost!"))
ctx.connection.on("reconnecting", lambda a, m: print(f"Retry {a}/{m}"))
ctx.connection.on("reconnected", lambda: print("Reconnected!"))
```

3. Check RabbitMQ logs for issues:

```bash
docker logs protobus-rabbitmq
```

## RPC Issues

### Request Timeout

**Symptom:** `asyncio.TimeoutError` after 10 minutes

**Possible causes:**
1. Service not running
2. Service queue not bound
3. Handler taking too long

**Solutions:**

1. Verify service is running and initialized:

```python
# Service side
print(f"Service {service.service_name} initialized")
```

2. Check queue exists in RabbitMQ Management UI (http://localhost:15672)

3. Reduce timeout for faster feedback:

```bash
export MESSAGE_PROCESSING_TIMEOUT=30000  # 30 seconds
```

### Method Not Found

**Symptom:** `InvalidMethodError: Invalid service method <name>`

**Possible causes:**
1. Method name mismatch
2. Method not defined on service

**Solutions:**

```python
# Ensure method exists on service class
class MyService(MessageService):
    async def my_method(self, data, actor, correlation_id):
        return {"result": "ok"}

# Client calls must match method name exactly
result = await proxy.my_method({"input": "data"})
```

### Response Parsing Error

**Symptom:** `InvalidResponseError`

**Solutions:**

1. Check service is returning valid dict:

```python
async def my_method(self, data, actor, correlation_id):
    return {"result": "ok"}  # Must be JSON-serializable dict
```

2. Enable debug logging to see raw messages:

```python
from protobus import set_logger

class DebugLogger:
    def debug(self, msg): print(f"[DEBUG] {msg}")
    def info(self, msg): print(f"[INFO] {msg}")
    def warn(self, msg): print(f"[WARN] {msg}")
    def error(self, msg): print(f"[ERROR] {msg}")

set_logger(DebugLogger())
```

## Event Issues

### Events Not Received

**Symptom:** Subscriber doesn't receive published events

**Possible causes:**
1. Subscriber not running when event published
2. Topic pattern mismatch
3. Wrong event type name

**Solutions:**

1. **Subscriber must be running first** - events are not queued if no subscribers

2. Check topic patterns match:

```python
# Publisher
await service.publish_event("OrderCreated", {"id": 123})
# Routes to: EVENT.OrderCreated

# Subscriber
await service.subscribe_event("OrderCreated", handler)
# Binds to: EVENT.OrderCreated ✓

# Or with custom topic
await service.subscribe_event("OrderCreated", handler, topic="EVENT.Order.*")
```

3. Use wildcard for debugging:

```python
async def debug_handler(data, topic):
    print(f"Received event on {topic}: {data}")

await service.subscribe_event("#", debug_handler)  # Catch all
```

### Duplicate Events

**Symptom:** Handler called multiple times for same event

**Possible causes:**
1. Multiple subscriptions
2. Handler error causing requeue

**Solutions:**

1. Ensure single subscription:

```python
# Don't call subscribe multiple times for same event type
await self.subscribe_event("MyEvent", self.handler)
# NOT: await self.subscribe_event("MyEvent", self.handler)  # Again
```

2. Handle errors properly:

```python
async def handler(self, data, topic):
    try:
        # Process event
        pass
    except Exception as e:
        # Log but don't re-raise to prevent requeue
        print(f"Error processing event: {e}")
```

## Performance Issues

### High Memory Usage

**Solutions:**

1. Limit concurrent processing:

```python
from protobus import MessageServiceOptions

service = MyService(ctx, MessageServiceOptions(
    max_concurrent=10  # Limit to 10 concurrent messages
))
```

2. Monitor queue depths in RabbitMQ Management UI

### Slow Response Times

**Solutions:**

1. Profile handler execution:

```python
import time

async def my_method(self, data, actor, correlation_id):
    start = time.time()
    result = await self._do_work(data)
    print(f"Handler took {time.time() - start:.2f}s")
    return result
```

2. Check for blocking operations:

```python
# BAD - blocking
def my_method(self, data, actor, correlation_id):
    time.sleep(5)  # Blocks event loop!

# GOOD - async
async def my_method(self, data, actor, correlation_id):
    await asyncio.sleep(5)  # Non-blocking
```

3. Optimize database queries and external calls

## Debugging Tools

### RabbitMQ Management UI

Access at http://localhost:15672 (guest/guest) to:
- View queue depths
- Check bindings
- Monitor message rates
- Inspect dead-letter queues

### Enable Debug Logging

```python
from protobus import set_logger
import logging

logging.basicConfig(level=logging.DEBUG)

class VerboseLogger:
    def debug(self, msg): print(f"[DEBUG] {msg}")
    def info(self, msg): print(f"[INFO] {msg}")
    def warn(self, msg): print(f"[WARN] {msg}")
    def error(self, msg): print(f"[ERROR] {msg}")

set_logger(VerboseLogger())
```

### Inspect Queues

```bash
# List queues
docker exec rabbitmq rabbitmqctl list_queues name messages

# List exchanges
docker exec rabbitmq rabbitmqctl list_exchanges name type

# List bindings
docker exec rabbitmq rabbitmqctl list_bindings
```

### Check Dead Letter Queue

```python
# Messages that failed all retries end up in DLQ
# Queue name: <service_name>.DLQ
# Check in RabbitMQ Management UI
```

---

Next: [Known Issues](known-issues.md) | [API Reference](api/context.md)
