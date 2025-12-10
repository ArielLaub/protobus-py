# Architecture Overview

Protobus is a Python microservices framework that enables services to communicate via RabbitMQ using Protocol Buffers for message serialization.

> **Note:** This is the Python port of [Protobus](https://github.com/ArielLaub/protobus). See the [original architecture docs](https://github.com/ArielLaub/protobus/blob/master/docs/architecture.md) for the TypeScript version.

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         Your Application                         │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │   Context   │  │  Services   │  │     Service Proxies     │  │
│  └──────┬──────┘  └──────┬──────┘  └────────────┬────────────┘  │
│         │                │                      │                │
├─────────┴────────────────┴──────────────────────┴────────────────┤
│                        Protobus Framework                        │
│  ┌────────────┐ ┌────────────────┐ ┌──────────────────────────┐  │
│  │ Connection │ │ MessageFactory │ │      Dispatchers         │  │
│  └─────┬──────┘ └───────┬────────┘ └────────────┬─────────────┘  │
├────────┴────────────────┴───────────────────────┴────────────────┤
│                          RabbitMQ (AMQP)                         │
└─────────────────────────────────────────────────────────────────┘
```

## Core Components

### Context

The central orchestrator that manages all other components:

```python
from protobus import Context, ContextOptions

options = ContextOptions(
    max_reconnect_attempts=10,
    initial_reconnect_delay_ms=1000,
    max_reconnect_delay_ms=30000
)

ctx = Context(options)
await ctx.init("amqp://guest:guest@localhost:5672/")
```

**Responsibilities:**
- Creates and manages the Connection
- Initializes MessageFactory
- Sets up MessageDispatcher and EventDispatcher
- Provides the public API for message publishing

### Connection

Manages the AMQP connection to RabbitMQ:

```python
# Connection is managed by Context, but you can access it:
connection = ctx.connection

# Check connection status
print(f"Connected: {connection.is_connected}")
print(f"Reconnecting: {connection.is_reconnecting}")
```

**Features:**
- Automatic reconnection with exponential backoff
- Channel management
- Queue and exchange declaration
- Message publishing and consuming

### MessageFactory

Handles Protocol Buffer encoding/decoding:

```python
# Access via context
factory = ctx.factory

# Build a request
request_bytes = factory.build_request(
    "calculator.MathService.add",
    {"a": 5, "b": 3},
    actor="client-1"
)

# Decode a response
response = factory.decode_response(response_bytes)
```

**Features:**
- Request/Response/Event container serialization
- Custom type support (BigInt, Timestamp)
- Proto file parsing

### MessageService

Base class for implementing RPC services:

```python
from protobus import MessageService

class MyService(MessageService):
    @property
    def service_name(self) -> str:
        return "mypackage.MyService"

    @property
    def proto_file_name(self) -> str:
        return "my_service.proto"

    async def my_method(self, data, actor, correlation_id):
        return {"result": "success"}
```

### ServiceProxy

Dynamic client for calling remote services:

```python
from protobus import ServiceProxy

proxy = ServiceProxy(ctx, "mypackage.MyService")
await proxy.init()

# Methods are created dynamically
result = await proxy.my_method({"input": "data"})
```

### ServiceCluster

Manages multiple service instances:

```python
from protobus import ServiceCluster

cluster = ServiceCluster(ctx)
cluster.use(ServiceA, count=2)  # 2 instances for load balancing
cluster.use(ServiceB, count=1)
await cluster.init()
```

## RabbitMQ Topology

### Exchanges

Protobus uses three exchanges:

| Exchange | Type | Purpose |
|----------|------|---------|
| `proto.bus` | topic | RPC requests routing |
| `proto.bus.callback` | direct | RPC responses |
| `proto.bus.events` | topic | Event pub/sub |

Configure via environment variables:
```bash
export BUS_EXCHANGE_NAME=proto.bus
export CALLBACKS_EXCHANGE_NAME=proto.bus.callback
export EVENTS_EXCHANGE_NAME=proto.bus.events
```

### Queues

**Service Queues:**
- Named after service (e.g., `calculator.MathService`)
- Durable and persistent
- Bound to main exchange with pattern `REQUEST.<service_name>.*`

**Callback Queues:**
- Auto-generated names
- Exclusive and temporary
- Used for RPC responses

**Retry Queues:**
- Named `<service>.retry`
- Messages with TTL that re-route to main exchange
- Created when retry is enabled

**Dead Letter Queues:**
- Named `<service>.DLQ`
- Stores messages that exhausted retries

## Message Containers

### RequestContainer

```python
@dataclass
class RequestContainer:
    method: str      # e.g., "calculator.MathService.add"
    data: Any        # The request payload
    actor: str       # Optional caller identifier
```

### ResponseContainer

```python
@dataclass
class ResponseContainer:
    method: str
    result: Optional[Any]   # Success result
    error: Optional[dict]   # Error info (message, code)
```

### EventContainer

```python
@dataclass
class EventContainer:
    type: str        # Event type name
    data: Any        # Event payload
    topic: str       # Routing topic
```

## Message Flow

### RPC Flow

```
Client                          Broker                          Service
  │                               │                               │
  │ build_request()               │                               │
  │ ─────────────────────────────>│                               │
  │                               │ route to service queue        │
  │                               │ ─────────────────────────────>│
  │                               │                               │ decode_request()
  │                               │                               │ handler()
  │                               │                               │ build_response()
  │                               │<───────────────────────────── │
  │                               │ route via callback exchange   │
  │<───────────────────────────── │                               │
  │ decode_response()             │                               │
```

### Event Flow

```
Publisher                       Broker                      Subscribers
  │                               │                               │
  │ publish_event()               │                               │
  │ ─────────────────────────────>│                               │
  │                               │ route by topic pattern        │
  │                               │ ─────────────────────────────>│ Handler A
  │                               │ ─────────────────────────────>│ Handler B
  │                               │ ─────────────────────────────>│ Handler C
```

## Concurrency Model

- **Default:** Unlimited concurrent message processing
- **Configurable:** Set `max_concurrent` in service options
- **Prefetch:** Uses RabbitMQ QoS to limit unacknowledged messages

```python
from protobus import MessageService, MessageServiceOptions

service = MyService(ctx, MessageServiceOptions(
    max_concurrent=10  # Process max 10 messages at once
))
```

## Reliability Features

### Message Persistence

- All messages are marked as persistent
- Survive broker restarts
- Service queues are durable

### Automatic Reconnection

- Exponential backoff (configurable)
- Jitter to prevent thundering herd
- Channel and consumer restoration

### Message Retry

- Configurable retry count and delay
- Dead letter queue for failed messages
- Retry headers track failure history

---

Next: [Configuration](configuration.md) | [Message Flow](message-flow.md)
