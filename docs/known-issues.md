# Known Issues

Current limitations and workarounds for Protobus Python.

> **Note:** This is the Python port of [Protobus](https://github.com/ArielLaub/protobus). See the [original known issues](https://github.com/ArielLaub/protobus/blob/master/docs/known-issues.md) for the TypeScript version.

## Graceful Shutdown

**Issue:** There is no built-in mechanism to gracefully shut down services.

**Workaround:** Handle shutdown at the application level:

```python
import asyncio
import signal
from protobus import Context

ctx = Context()
shutdown_event = asyncio.Event()

async def shutdown(signal_name):
    print(f"Received {signal_name}, shutting down...")
    shutdown_event.set()

async def main():
    # Set up signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(shutdown(s.name))
        )

    await ctx.init("amqp://guest:guest@localhost:5672/")

    # Start your services...
    service = MyService(ctx)
    await service.init()

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Clean up
    print("Closing connection...")
    await ctx.close()
    print("Shutdown complete")

if __name__ == "__main__":
    asyncio.run(main())
```

## Request Tracing

**Issue:** No built-in distributed tracing support.

**Workaround:** Implement tracing manually using the `actor` parameter or custom headers:

```python
import uuid

# Client side - pass trace ID as actor
trace_id = str(uuid.uuid4())
result = await proxy.my_method({"data": "value"}, actor=trace_id)

# Service side - log with trace ID
async def my_method(self, data, actor, correlation_id):
    print(f"[{actor}] Processing request {correlation_id}")
    # ... do work ...
    return {"result": "ok"}
```

For full tracing with OpenTelemetry:

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

trace.set_tracer_provider(TracerProvider())
tracer = trace.get_tracer(__name__)

async def my_method(self, data, actor, correlation_id):
    with tracer.start_as_current_span("my_method") as span:
        span.set_attribute("correlation_id", correlation_id)
        span.set_attribute("actor", actor or "unknown")
        # ... do work ...
        return {"result": "ok"}
```

## Event Unsubscribe

**Issue:** The Trie data structure doesn't support removing subscriptions.

**Workaround:** Use conditional handling in your event handlers:

```python
class MyService(MessageService):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._event_enabled = True

    async def on_event(self, data, topic):
        if not self._event_enabled:
            return  # Skip if disabled

        # Process event...

    def disable_event_handling(self):
        self._event_enabled = False
```

## Proto File Compilation

**Issue:** The Python port uses JSON encoding instead of actual Protocol Buffer compilation for simplicity.

**Impact:**
- Messages are larger than binary protobuf
- No compile-time type checking from proto files

**Workaround:** For production use with strict requirements, you can:

1. Use `grpcio-tools` to compile proto files to Python
2. Create custom message classes
3. Override the MessageFactory encoding methods

```python
# Example: Custom protobuf integration (advanced)
from google.protobuf import json_format

class MyService(MessageService):
    async def my_method(self, data, actor, correlation_id):
        # Convert dict to protobuf message
        proto_msg = MyProtoMessage()
        json_format.ParseDict(data, proto_msg)

        # Process...

        # Convert result back to dict
        return json_format.MessageToDict(result_proto)
```

## Maximum Message Size

**Issue:** Very large messages may cause issues.

**Workaround:**
- RabbitMQ default max is 128MB
- Configure in rabbitmq.conf if needed
- Consider chunking for very large payloads

## Connection Pool

**Issue:** Each Context maintains a single connection.

**Workaround:** For high-throughput applications, create multiple contexts:

```python
contexts = [Context() for _ in range(5)]
for ctx in contexts:
    await ctx.init("amqp://...")

# Use contexts in a pool
```

## Reporting Issues

When reporting bugs, please include:

1. Protobus Python version
2. Python version
3. RabbitMQ version
4. Steps to reproduce
5. Error messages and stack traces
6. Relevant configuration

File issues at: https://github.com/ArielLaub/protobus-py/issues

---

For the TypeScript version issues, see: https://github.com/ArielLaub/protobus/issues
