# Configuration

Protobus is configured through environment variables, constructor parameters, and reconnection options.

## Environment Variables

Every variable is read through [`protobus/config.py`](../../protobus/config.py) at the moment it is needed, not at import, so a test can set one with `monkeypatch.setenv` and see it take effect. An integer variable that is malformed, empty, zero or negative silently falls back to its default rather than crashing the process.

### Exchanges

| Variable | Default | Description |
|----------|---------|-------------|
| `BUS_EXCHANGE_NAME` | `proto.bus` | Main exchange for RPC requests |
| `CALLBACKS_EXCHANGE_NAME` | `proto.bus.callback` | Exchange for RPC responses |
| `EVENTS_EXCHANGE_NAME` | `proto.bus.events` | Exchange for pub/sub events |
| `CANCEL_EXCHANGE_NAME` | `proto.bus.cancel` | Fanout exchange carrying stream-cancellation notices |

### Connection

| Variable | Default | Description |
|----------|---------|-------------|
| `AMQP_HEARTBEAT_SECONDS` | `30` | AMQP heartbeat interval. See [Heartbeats](#heartbeats) below |
| `CONNECTION_READY_TIMEOUT_MS` | `30000` | How long a publish parked on a reconnection waits before failing with `NotReadyError` |

### Timeouts

| Variable | Default | Description |
|----------|---------|-------------|
| `MESSAGE_PROCESSING_TIMEOUT` | `600000` | How long a **server** handler may run before the delivery is abandoned (10 minutes) |
| `RPC_CALL_TIMEOUT_MS` | `600000` | How long a **caller** waits for a reply before `RpcTimeoutError` (10 minutes) |
| `STREAM_IDLE_TIMEOUT_MS` | `60000` | Longest gap between streaming chunks before `StreamTimeoutError` |
| `PUBLISH_CONFIRM_TIMEOUT_MS` | `30000` | How long a publish waits for its broker confirm |
| `SHUTDOWN_DRAIN_TIMEOUT_MS` | `30000` | How long a graceful shutdown waits for in-flight messages |

The two 10-minute defaults are separate settings for separate roles, and they
are not the same clock. A caller gives up after `RPC_CALL_TIMEOUT_MS`, while a
server may still be retrying the same request for
`max_retries × (MESSAGE_PROCESSING_TIMEOUT + retry_delay_ms)`. Set the caller's
budget deliberately rather than leaving both at the default.

### Throughput and bounds

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_PREFETCH` | `1` | Unacked messages a late-ack consumer will hold when `max_concurrent` is unset. See [Concurrency](#concurrency) |
| `MAX_OUTSTANDING_CONFIRMS` | `256` | Publishes awaiting a confirm on one channel before further publishes park |
| `STREAM_MAX_BUFFERED_CHUNKS` | `1024` | Chunks buffered for one unconsumed streaming call |
| `STREAM_MAX_BUFFERED_BYTES` | `67108864` | Bytes buffered for one unconsumed streaming call (64 MiB) |
| `STREAM_MAX_TOTAL_BUFFERED_BYTES` | `268435456` | Bytes buffered across every streaming call on one dispatcher (256 MiB) |

### Errors and logging

| Variable | Default | Description |
|----------|---------|-------------|
| `PROTOBUS_EXPOSE_INTERNAL_ERRORS` | `true` | Send an unhandled error's message back to the caller. See [Security](../operations/security.md) |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warn`, `error`, or `silent` |

### Concurrency

`max_concurrent` is the consumer prefetch, and it defaults to **1**: a replica
handles one request at a time, holding the slot until the handler returns and
its reply is away.

For unary handlers that is a deliberate, conservative default. **For streaming
it is usually wrong.** A streaming handler keeps its slot for the entire life
of the stream, so a service answering minute-long token streams with the
default serves exactly one caller per replica and queues everyone else:

```python
# A streaming service almost always wants this set.
service = AssistantService(context, max_concurrent=8)
```

Raise it to the number of concurrent messages one replica should be working on.
It bounds memory as well as throughput — with late ack, the broker will push up
to this many unacknowledged messages into the process. Each in-flight message
runs as its own asyncio task; a handler that blocks the loop blocks them all.

### Heartbeats

The broker closes a connection after two missed heartbeats, so the interval is
half the worst-case time to notice a peer that vanished without closing its
socket — a crashed broker, a network partition, a NAT that dropped the flow.
At the default of 30 seconds that is about a minute.

Left to the broker to propose, RabbitMQ asks for 60 seconds, which is two
minutes of publishing into a dead socket while the connection still reports
itself healthy. That is why protobus sets one rather than accepting the
proposal.

A heartbeat already present in the connection URL is treated as deliberate and
left alone, which is also how you turn heartbeats off:

```
amqp://guest:guest@localhost:5672/?heartbeat=0
```

Shortening the interval detects failure sooner at the cost of a few extra
frames per minute per connection. Raising it above the broker's own
`heartbeat` setting has no effect — the lower of the two is negotiated.

## Reconnection Options

Protobus automatically reconnects when the RabbitMQ connection is lost. Configure reconnection behavior when initializing the context:

```python
from protobus import Context, ContextOptions, ReconnectionOptions

reconnection = ReconnectionOptions(
    max_retries=10,           # Max attempts (0 = infinite)
    initial_delay_ms=1000,    # First retry delay
    max_delay_ms=30000,       # Max delay between retries
    backoff_multiplier=2.0,   # Exponential backoff multiplier
)

context = Context()
await context.init(amqp_url, proto_paths, ContextOptions(reconnection=reconnection))
```

### Reconnection Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_retries` | `10` | Maximum reconnection attempts. Set to `0` for infinite retries. |
| `initial_delay_ms` | `1000` | Delay before first reconnection attempt (ms). |
| `max_delay_ms` | `30000` | Maximum delay between attempts (ms). |
| `backoff_multiplier` | `2.0` | Multiplier for exponential backoff. |

### Reconnection Behavior

1. **Connection loss detected** - All channels become invalid
2. **Pending RPC calls fail** - With `DisconnectedError`, except a request whose publish was never confirmed, which is republished once on the restored channel
3. **Exponential backoff** - Delay doubles after each failed attempt, with up to 30% jitter
4. **Automatic re-initialization** - Channels, queues, bindings and consumers are restored by each component's registered *restorer*, before the connection announces itself reconnected
5. **Services resume** - Once reconnected, services continue processing

### Connection Events

Monitor connection state via the connection object:

```python
def on_disconnected() -> None:
    print("Connection lost")


def on_reconnecting(info: dict) -> None:
    print(f"Reconnecting (attempt {info['attempt']}, delay {info['delay']}ms)")


def on_reconnected() -> None:
    print("Connection restored")


def on_error(err: BaseException) -> None:
    print("Connection error:", err)


context.connection.on("disconnected", on_disconnected)
context.connection.on("reconnecting", on_reconnecting)
context.connection.on("reconnected", on_reconnected)
context.connection.on("error", on_error)

# Check connection state
if context.is_connected:
    ...  # Safe to make RPC calls

if context.is_reconnecting:
    ...  # Currently attempting to reconnect

# Or wait for it: returns when the connection is usable, raises NotReadyError
# after CONNECTION_READY_TIMEOUT_MS.
await context.connection.when_ready()
```

### Handling Disconnections in Client Code

```python
from protobus import DisconnectedError, ServiceProxy

try:
    result = await proxy.someMethod({"data": "test"})
except DisconnectedError:
    # Connection was lost during the RPC call, after the request was
    # confirmed. The system is automatically reconnecting; the server may
    # still complete the work. Retry after reconnection if the call is
    # idempotent.
    print("Connection lost, will retry after reconnection")
```

### Infinite Retries

For services that should never give up:

```python
await context.init(amqp_url, proto_paths, ContextOptions(
    reconnection=ReconnectionOptions(
        max_retries=0,        # Infinite retries
        max_delay_ms=60000,   # Cap at 1 minute between attempts
    ),
))
```

### Example

```bash
export BUS_EXCHANGE_NAME=myapp.bus
export CALLBACKS_EXCHANGE_NAME=myapp.bus.callback
export EVENTS_EXCHANGE_NAME=myapp.bus.events
export MESSAGE_PROCESSING_TIMEOUT=30000  # 30 seconds
```

## AMQP Connection String

The connection string follows the standard AMQP URI format:

```
amqp://[username:password@]host[:port][/vhost]
```

### Examples

```python
# Local development
url = "amqp://guest:guest@localhost:5672/"

# With virtual host
url = "amqp://user:password@rabbitmq.example.com:5672/production"

# CloudAMQP
url = "amqps://user:password@rabbit.cloudamqp.com/vhost"

# Amazon MQ
url = "amqps://user:password@b-xxx.mq.region.amazonaws.com:5671"
```

The URL is redacted before it reaches any log line (`amqp://user:***@host`).

## Context Initialization

```python
context = Context()
await context.init(amqp_url, proto_paths)
```

**Parameters:**
- `amqp_connection_string`: AMQP connection string
- `proto_locations`: a directory, a file, or a list of either — directories are searched recursively for `.proto` files. `proto_dirs=` is the 1.x keyword for the same argument.
- `options`: an optional `ContextOptions` carrying `reconnection`

## Service Configuration

### MessageService Options

```python
class MyService(MessageService):
    # Required: Service identifier
    service_name = "Package.ServiceName"

    # Path to the proto file. Only read if Context.init() did not already
    # load a schema declaring the service.
    proto_file_name = os.path.join(os.path.dirname(__file__), "service.proto")


# Concurrency is a constructor option, not an override. Default 1.
service = MyService(context, max_concurrent=10)
# or
service = MyService(context, MessageServiceOptions(max_concurrent=10))
```

`MessageServiceOptions` fields: `max_concurrent`, `retry` (a `RetryOptions`), `late_ack` (default `True`), `processing_timeout_ms`, `max_priority`, `event_retry` (an `EventRetryOptions`). Every field is also accepted as a keyword argument by the constructor and by `RunnableService.start()` / `launch()`. Full reference: [MessageService](./api/message-service.md).

## Queue Configuration

Protobus creates queues with the following defaults:

### Service Queues
- **Name:** `<service_name>` (e.g., `Calculator.Math`)
- **Durable:** `true` - survives broker restart
- **Auto-delete:** `false`
- **Exclusive:** `false`
- **Arguments:** `x-message-ttl` when `message_ttl_ms` is set; `x-max-priority` when `max_priority` is set; otherwise none

### Callback Queues
- **Name:** Auto-generated unique ID
- **Durable:** `false`
- **Auto-delete:** `true` - deleted when client disconnects
- **Exclusive:** `true` - only accessible by creating connection

### Event Queues
- **Name:** `<service_name>.Events` (e.g., `Calculator.Math.Events`)
- **Durable:** `true`
- **Auto-delete:** `false`

### Retry and dead-letter queues

Declared only when `max_retries` is above zero: `<service_name>.Retry` (with `x-message-ttl: retry_delay_ms` and `x-dead-letter-exchange: proto.bus`) and `<service_name>.DLQ`; with `event_retry`, the same shapes under `<service_name>.Events.`. See [Architecture](../concepts/architecture.md).

## Exchange Configuration

All exchanges are created with:
- **Type:** `topic` (main, events, retry) · `direct` (callback) · `fanout` (cancel)
- **Durable:** `true`
- **Auto-delete:** `false`

## Message Options

### Persistence
Requests and events are sent persistent (`delivery_mode` 2), ensuring they survive broker restarts. Replies are not: the callback queue they land on is transient by design.

### Timeout
A server handler is cancelled after `MESSAGE_PROCESSING_TIMEOUT` milliseconds, or the service's own `processing_timeout_ms`. Adjust this for long-running operations:

```bash
# For operations that may take up to 5 minutes
export MESSAGE_PROCESSING_TIMEOUT=300000
```

## Logging Configuration

Replace the default logger:

```python
import logging

from protobus import set_logger

log = logging.getLogger("protobus")


class StdlibLogger:
    def debug(self, message): log.debug(message)
    def info(self, message): log.info(message)
    def warn(self, message): log.warning(message)
    def error(self, message): log.error(message)


set_logger(StdlibLogger())
```

See [Logging](../operations/logging.md) for structured records and the diagnostics serializer.

## RabbitMQ Server Configuration

Recommended RabbitMQ settings for production:

```ini
# rabbitmq.conf

# Upper bound on the heartbeat interval. The lower of this and the client's
# AMQP_HEARTBEAT_SECONDS is what gets negotiated, so raising this alone does
# not slow protobus's heartbeats down.
heartbeat = 60

# Memory high watermark
vm_memory_high_watermark.relative = 0.7

# Disk free limit
disk_free_limit.relative = 2.0
```

## Docker Compose Example

```yaml
services:
  rabbitmq:
    image: rabbitmq:3-management
    ports:
      - "5672:5672"
      - "15672:15672"
    environment:
      RABBITMQ_DEFAULT_USER: protobus
      RABBITMQ_DEFAULT_PASS: secret
    volumes:
      - rabbitmq_data:/var/lib/rabbitmq

  my-service:
    build: .
    environment:
      AMQP_URL: amqp://protobus:secret@rabbitmq:5672/
      BUS_EXCHANGE_NAME: myapp.bus
      MESSAGE_PROCESSING_TIMEOUT: 60000
      PYTHONUNBUFFERED: "1"
    depends_on:
      - rabbitmq

volumes:
  rabbitmq_data:
```

---

Next: [Message Flow](../concepts/message-flow.md) | [Architecture](../concepts/architecture.md)
