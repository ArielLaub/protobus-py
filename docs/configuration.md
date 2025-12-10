# Configuration

This guide covers all configuration options for Protobus Python.

> **Note:** This is the Python port of [Protobus](https://github.com/ArielLaub/protobus). See the [original configuration docs](https://github.com/ArielLaub/protobus/blob/master/docs/configuration.md) for the TypeScript version.

## Environment Variables

### Exchange Names

| Variable | Default | Description |
|----------|---------|-------------|
| `BUS_EXCHANGE_NAME` | `proto.bus` | Main exchange for RPC requests |
| `CALLBACKS_EXCHANGE_NAME` | `proto.bus.callback` | Exchange for RPC responses |
| `EVENTS_EXCHANGE_NAME` | `proto.bus.events` | Exchange for pub/sub events |
| `MESSAGE_PROCESSING_TIMEOUT` | `600000` | RPC timeout in milliseconds (10 min) |

Example:

```bash
export BUS_EXCHANGE_NAME=myapp.bus
export CALLBACKS_EXCHANGE_NAME=myapp.callbacks
export EVENTS_EXCHANGE_NAME=myapp.events
export MESSAGE_PROCESSING_TIMEOUT=30000  # 30 seconds
```

## Context Options

Configure the context when creating it:

```python
from protobus import Context, ContextOptions

options = ContextOptions(
    max_reconnect_attempts=10,        # Max reconnection tries (0 = infinite)
    initial_reconnect_delay_ms=1000,  # First retry delay (1 second)
    max_reconnect_delay_ms=30000,     # Max delay cap (30 seconds)
    reconnect_backoff_multiplier=2.0  # Exponential backoff multiplier
)

ctx = Context(options)
await ctx.init("amqp://guest:guest@localhost:5672/")
```

### Reconnection Strategy

The reconnection uses exponential backoff with jitter:

```
delay = min(initial_delay * (multiplier ^ attempt), max_delay)
actual_delay = delay * (1 + random(-0.3, 0.3))  # 30% jitter
```

For infinite retries (services that must stay connected):

```python
options = ContextOptions(
    max_reconnect_attempts=0,  # 0 = infinite
    initial_reconnect_delay_ms=1000,
    max_reconnect_delay_ms=60000
)
```

## Connection Events

Monitor connection status:

```python
ctx = Context()

# Register event handlers before init
ctx.connection.on("disconnected", lambda: print("Lost connection!"))
ctx.connection.on("reconnecting", lambda a, m: print(f"Reconnecting {a}/{m}"))
ctx.connection.on("reconnected", lambda: print("Reconnected!"))
ctx.connection.on("error", lambda e: print(f"Error: {e}"))

await ctx.init("amqp://...")
```

### Connection State

Check current state:

```python
if ctx.is_connected:
    print("Ready to process messages")

if ctx.is_reconnecting:
    print("Attempting to reconnect...")
```

## Service Options

Configure individual services:

```python
from protobus import MessageService, MessageServiceOptions, RetryOptions

options = MessageServiceOptions(
    max_concurrent=10,  # Max parallel message processing
    retry=RetryOptions(
        max_retries=3,        # Retry failed messages 3 times
        retry_delay_ms=5000,  # 5 second delay between retries
        message_ttl_ms=None   # Optional message TTL
    )
)

service = MyService(ctx, options)
```

### Retry Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `max_retries` | 3 | Max retry attempts (0 = no retry) |
| `retry_delay_ms` | 5000 | Delay between retries (ms) |
| `message_ttl_ms` | None | Message expiration (None = no expiry) |

### Disabling Retry

```python
options = MessageServiceOptions(
    retry=RetryOptions(max_retries=0)  # No retries
)
```

## Connection URL

The AMQP URL follows standard format:

```
amqp://username:password@hostname:port/vhost
```

Examples:

```python
# Local development
await ctx.init("amqp://guest:guest@localhost:5672/")

# With virtual host
await ctx.init("amqp://user:pass@rabbit.example.com:5672/myapp")

# Cloud/Production (with SSL)
await ctx.init("amqps://user:pass@rabbit.cloud.com:5671/production")
```

## Logging Configuration

Replace the default logger:

```python
from protobus import set_logger
import logging

class MyLogger:
    def __init__(self):
        self._logger = logging.getLogger("myapp")

    def info(self, message):
        self._logger.info(message)

    def warn(self, message):
        self._logger.warning(message)

    def debug(self, message):
        self._logger.debug(message)

    def error(self, message):
        self._logger.error(message)

set_logger(MyLogger())
```

### Suppressing Debug Output

```python
class QuietLogger:
    def info(self, message):
        print(f"[INFO] {message}")

    def warn(self, message):
        print(f"[WARN] {message}")

    def debug(self, message):
        pass  # Suppress debug

    def error(self, message):
        print(f"[ERROR] {message}")

set_logger(QuietLogger())
```

## Production Recommendations

### RabbitMQ Settings

```yaml
# docker-compose.yml
services:
  rabbitmq:
    image: rabbitmq:3-management
    environment:
      RABBITMQ_DEFAULT_USER: myuser
      RABBITMQ_DEFAULT_PASS: securepassword
      # Heartbeat for detecting dead connections
      RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS: "-rabbit heartbeat 30"
    ports:
      - "5672:5672"
      - "15672:15672"
```

### Service Configuration

```python
# Production settings
options = ContextOptions(
    max_reconnect_attempts=0,         # Infinite retries
    initial_reconnect_delay_ms=1000,
    max_reconnect_delay_ms=60000,     # Cap at 1 minute
)

service_options = MessageServiceOptions(
    max_concurrent=20,                # Limit concurrency
    retry=RetryOptions(
        max_retries=5,
        retry_delay_ms=10000,         # 10 second retry delay
    )
)
```

### Health Checks

```python
async def health_check():
    return {
        "status": "healthy" if ctx.is_connected else "unhealthy",
        "reconnecting": ctx.is_reconnecting
    }
```

## Error Handling

Handle connection errors in your application:

```python
from protobus import DisconnectedError

try:
    result = await proxy.some_method(data)
except DisconnectedError:
    # Connection lost during RPC
    print("Connection lost, retrying...")
    await asyncio.sleep(1)
    # Retry logic...
except asyncio.TimeoutError:
    # RPC timed out
    print("Request timed out")
```

---

Next: [Message Flow](message-flow.md) | [Troubleshooting](troubleshooting.md)
