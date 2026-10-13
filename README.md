<p align="center">
  <img src="https://raw.githubusercontent.com/ArielLaub/protobus-py/main/docs/assets/banner.jpg" alt="ProtoBus: many languages, one message bus. A thin layer over Protocol Buffers and RabbitMQ." width="100%">
</p>

# ProtoBus for Python

**RabbitMQ-native microservices for Python, with Protocol Buffers on the wire.**

[![PyPI](https://img.shields.io/pypi/v/protobus.svg?logo=pypi)](https://pypi.org/project/protobus/)
[![python](https://img.shields.io/badge/python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![RabbitMQ](https://img.shields.io/badge/RabbitMQ-%E2%89%A53.8-FF6600?logo=rabbitmq&logoColor=white)](https://www.rabbitmq.com)
[![CI](https://github.com/ArielLaub/protobus-py/actions/workflows/ci.yml/badge.svg)](https://github.com/ArielLaub/protobus-py/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/ArielLaub/protobus-py/blob/main/LICENSE)

Define a service in a `.proto` file, implement it as a class, and call it from
anywhere on the bus as if it were local. ProtoBus turns each service into **one
durable RabbitMQ queue with N processes competing for it**, so load balancing,
failover, backpressure, retries and dead-lettering are handled by the broker
instead of in Python.

This is the Python port of [protobus](https://github.com/ArielLaub/protobus)
(TypeScript). The ports are **wire-compatible**: a Python service serves callers
written for any of them and the other way round, with streaming, events, custom
types and error codes included. They are listed under
[Wire compatibility](#wire-compatibility).

**Status: stable.** 2.0.0 is the current release. Every commit runs a
cross-language suite in both directions against a live broker and a pinned
TypeScript revision (2.5.0); the behavioural differences that remain are listed
in [Known Issues](https://github.com/ArielLaub/protobus-py/blob/main/docs/operations/known-issues.md#differences-from-the-typescript-port).

---

## Install

```bash
pip install protobus
```

No `protoc` is needed: `.proto` files are parsed at runtime. `protobus
generate` writes Python typing for them when you want it (see
[CLI](https://github.com/ArielLaub/protobus-py/blob/main/docs/reference/cli.md)).
The runtime depends on [aiormq](https://github.com/mosquito/aiormq) for AMQP
and [protobuf](https://pypi.org/project/protobuf/) for the wire.

You also need a RabbitMQ 3.8+ broker:

```bash
docker run -d --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3-management-alpine
export AMQP_URL=amqp://guest:guest@localhost:5672/
```

---

## Quick start

Four steps to a working RPC.

### 1. Describe the service

```protobuf
// proto/Calculator.proto
syntax = "proto3";
package Calculator;

message AddRequest {
    int32 a = 1;
    int32 b = 2;
}

message AddResponse {
    int32 result = 1;
}

service Math {
    rpc add(Calculator.AddRequest) returns(Calculator.AddResponse);
}
```

The package plus the service name is the service's name on the bus:
`Calculator.Math`. The file is an ordinary protobus schema, shared as-is with
the other languages.

### 2. Implement it

```python
# calculator_service.py
from protobus import RunnableService


class CalculatorService(RunnableService):
    service_name = "Calculator.Math"

    async def add(self, request: dict, actor: str, correlation_id: str) -> dict:
        return {"result": request["a"] + request["b"]}
```

### 3. Run it

```python
# server.py
import asyncio, os
from protobus import Context
from calculator_service import CalculatorService


async def main():
    context = Context()
    await context.init(os.environ.get("AMQP_URL", "amqp://localhost"), ["./proto"])
    await CalculatorService.start(context)   # blocks until SIGINT/SIGTERM


asyncio.run(main())
```

`RunnableService.start` installs signal handlers and, on shutdown, stops
taking new work, drains in-flight messages, runs your `cleanup()` hook and
disconnects, in that order.

### 4. Call it

```python
# client.py
import asyncio, os
from protobus import Context, ServiceProxy


async def main():
    context = Context()
    await context.init(os.environ.get("AMQP_URL", "amqp://localhost"), ["./proto"])

    calculator = ServiceProxy(context, "Calculator.Math")
    await calculator.init()

    response = await calculator.add({"a": 5, "b": 3})
    print(f"5 + 3 = {response['result']}")

    await context.close()


asyncio.run(main())
```

```
$ python client.py
5 + 3 = 8
```

A call is bounded by `RPC_CALL_TIMEOUT_MS` (10 minutes by default, as in the
other ports) unless you pass `timeout_ms`.

The full walkthrough adds events, error handling and the project layout:
**[Getting Started](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/getting-started.md)**.

---

## Why ProtoBus

### RabbitMQ only, on purpose

ProtoBus is built for one broker, so the things a broker is good at stay in the
broker instead of being reimplemented above it:

| Concern | Where it lives |
|---|---|
| Load balancing | competing consumers on one queue |
| Routing | topic exchange bindings (`REQUEST.<Service>.*`) |
| Redelivery on consumer loss | late ack: an unacked delivery returns to the queue |
| Retry delay | the retry queue's `x-message-ttl`, drained by a dead-letter exchange |
| Persistence | durable queues, persistent messages |
| Dead letters | a real `<Service>.DLQ` |
| Priority | native queue priorities |

A request goes publisher → exchange → queue → consumer. Nothing tracks live
instances, so nothing holds a stale one, and a consumer that dies mid-request
leaves its delivery unacked for the next consumer to take.

The cost of this is written down rather than glossed over; read
[Delivery Guarantees](https://github.com/ArielLaub/protobus-py/blob/main/docs/concepts/delivery-guarantees.md)
before you rely on any of it. If you may need to swap RabbitMQ for another
broker, use a transport-agnostic framework instead. That is a real feature and
protobus does not have it.

### Protocol Buffers, not JSON

- **Contract-first.** The `.proto` file is the interface between teams and
  languages, and generated typing (`protobus generate`) tells you when the two
  drift apart.
- **Versioning by field number.** Adding a field does not break an old peer.
- **Usually smaller on the wire**, since fields travel as numbers and packed
  binary rather than text.

### A small protocol, a small port

The protocol protobus adds on top of AMQP is small and documented: five
envelope messages, a routing-key convention, an error encoding and two
streaming headers. Because queueing, consumer distribution and retry delays
belong to the broker, a port adapts that protocol to another AMQP client
rather than reimplementing messaging. This port has two runtime dependencies,
[aiormq](https://github.com/mosquito/aiormq) for AMQP and
[protobuf](https://pypi.org/project/protobuf/) for the wire, and brings no
framework: a service is a class with one `async def` per rpc.

---

## Features

- **Retries and dead-lettering.** An unhandled exception or a processing
  timeout parks the request on `<Service>.Retry` and redelivers it; after
  `max_retries` (3 by default, 5 seconds apart) it lands on `<Service>.DLQ`,
  and the caller is answered with the final error. A `HandledError` is an
  answer and is never retried. See
  [Error Handling](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/error-handling.md).
- **Server streaming.** A method declared `returns (stream T)` is implemented
  as an async generator and consumed with `async for`. Closing the stream
  early, or firing an abort signal, stops the producer on the server. See
  [Streaming](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/streaming.md).
- **Events.** `publish_event` publishes on a topic exchange; `subscribe_event`
  receives events by message type, topic pattern (`*`, `#`) or both, with
  optional per-listener retries and a dead-letter queue. See
  [Events](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/events.md).
- **Custom types.** `bigint` (unsigned 256-bit, decoded to `int`) and
  `timestamp` (milliseconds since the epoch, decoded to a timezone-aware
  `datetime`) are built in as `BigIntType` and `TimestampType`, already
  registered. Register your own `CustomType` and it becomes a field type in
  your schemas. See
  [Custom Types](https://github.com/ArielLaub/protobus-py/blob/main/docs/reference/custom-types.md).
- **Priority.** `max_priority` makes a service queue a priority queue, and a
  call's `priority` lets a control message overtake a bulk backlog. See
  [Priority](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/priority.md).
- **Instance-named services.** Many instances can serve one contract under
  their own names (`Combat.Player.player6`), each addressed by its own proxy.
- **Processing timeout.** `processing_timeout_ms` caps one attempt at a unary
  request; the handler's `context.signal` fires and the attempt counts as
  failed.
- **Reconnection.** A lost connection is re-established with capped
  exponential backoff, and every queue, binding and consumer is restored
  before the connection reports itself ready. Calls made meanwhile wait for
  it; calls in flight when it dropped fail with `DisconnectedError`.
- **Publisher confirms.** Every publish waits for the broker's confirm. A
  failure is a `PublishError` that says whether the outcome is definite or
  ambiguous, and a caller-supplied `message_id` makes a republish safe to
  deduplicate. This is at-least-once territory, written down in
  [Delivery Guarantees](https://github.com/ArielLaub/protobus-py/blob/main/docs/concepts/delivery-guarantees.md).
- **Logging, your way.** Structured records for your own sink, or plain lines
  through the `logging` module. See
  [Logging](https://github.com/ArielLaub/protobus-py/blob/main/docs/operations/logging.md).
- **CLI.** `protobus generate` writes Python typing (`TypedDict` / `Protocol`)
  from your schemas, `protobus generate:service Name` a runnable service stub,
  configured from `[tool.protobus]` in `pyproject.toml`. See
  [CLI](https://github.com/ArielLaub/protobus-py/blob/main/docs/reference/cli.md).
- **Graceful shutdown.** `RunnableService` stops intake on SIGINT/SIGTERM, lets
  in-flight work finish, runs your `cleanup()`, then disconnects.

### A short tour

```python
from protobus import Config, Context, RetryOptions, RunnableService, ServiceProxy


class CalculatorService(RunnableService):
    service_name = "Calculator.Math"

    async def add(self, request: dict, actor: str, correlation_id: str) -> dict:
        return {"result": request["a"] + request["b"]}


async def on_calculated(event: dict, event_type: str, topic: str) -> None:
    print(event["operation"])


async def tour(context: Context) -> None:
    # Options on a service: concurrency, retries, a processing timeout and a
    # priority queue. launch() returns the running service; start() blocks.
    service = await CalculatorService.launch(
        context,
        max_concurrent=16,
        retry=RetryOptions(max_retries=5, retry_delay_ms=10000),
        processing_timeout_ms=30000,
        max_priority=Config.RECOMMENDED_MAX_PRIORITY,
    )

    # Options on a call: an actor, a timeout and a priority.
    calculator = ServiceProxy(context, "Calculator.Math")
    await calculator.init()
    await calculator.add({"a": 1, "b": 2}, "billing-job", True, 5000, priority=Config.PRIORITY_HIGH)

    # Events: subscribe by type, publish from anywhere on the bus.
    await service.subscribe_event("Calculator.Calculated", on_calculated)
    await context.publish_event("Calculator.Calculated", {"operation": "add"})
```

```python
from protobus import MessageService


# Streaming, server side: an async generator; each yield is one chunk.
class Assistant(MessageService):
    service_name = "Chat.Assistant"
    proto_file_name = "chat.proto"

    async def generate(self, request, actor, correlation_id, context):
        for index, text in enumerate(["hello", "from", "python"]):
            if context.signal.aborted:      # the caller has gone: stop producing
                return
            yield {"index": index, "text": text}


# Client side: `async for` over the proxy's stream; leaving the `async with`
# block tells the server to stop.
async def read(assistant):
    async with assistant.generate({"prompt": "hi"}) as stream:
        async for token in stream:
            print(token["text"], end=" ")
            if token["index"] == 1:
                break
```

---

## Concurrency

One process is one asyncio event loop. Handlers run concurrently as
coroutines, never in parallel, so they share state freely; a handler that
blocks the loop (a synchronous driver, `time.sleep`, a CPU-bound loop) stalls
every other in-flight message on that replica, so hand blocking work to
`asyncio.to_thread` or run more replicas. The number in flight is bounded by
the consumer's prefetch: `max_concurrent` for a service (default 1, one
request at a time), `DEFAULT_PREFETCH` for event handling (also 1). A
streaming handler holds its slot for the life of its stream.

Create one `Context` per process and share it between services and proxies.
See
[Architecture](https://github.com/ArielLaub/protobus-py/blob/main/docs/concepts/architecture.md)
and
[Patterns](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/patterns.md).

---

## Wire compatibility

protobus-py speaks the protobus wire protocol exactly as TypeScript protobus
2.5 and the other ports do: the same exchanges, queues, envelopes (byte for
byte), headers and error codes, and the same environment variables for
configuration. Replicas of one service in different languages can share its
queue and climb one retry ladder together. The cross-language suite runs
Python against the TypeScript library over a real broker, in both directions,
and every newer port runs its own suite against this one.

| Language | Repo | Status |
|---|---|---|
| TypeScript | [protobus](https://github.com/ArielLaub/protobus) | stable (reference) |
| Python | [protobus-py](https://github.com/ArielLaub/protobus-py) (this repository) | stable |
| Go | [protobus-go](https://github.com/ArielLaub/protobus-go) | stable |
| C++ | [protobus-cpp](https://github.com/ArielLaub/protobus-cpp) | new |
| Java | [protobus-java](https://github.com/ArielLaub/protobus-java) | new |
| C# | [protobus-csharp](https://github.com/ArielLaub/protobus-csharp) | new |
| Rust | [protobus-rs](https://github.com/ArielLaub/protobus-rs) | new |

The wire format and the round trip are in
**[Message Flow](https://github.com/ArielLaub/protobus-py/blob/main/docs/concepts/message-flow.md)**;
the behavioural differences from the TypeScript port are in
**[Known Issues](https://github.com/ArielLaub/protobus-py/blob/main/docs/operations/known-issues.md#differences-from-the-typescript-port)**.

---

## Documentation

Full index: **[docs/](https://github.com/ArielLaub/protobus-py/blob/main/docs/README.md)**

| Start | |
|---|---|
| [Getting Started](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/getting-started.md) | zero to a working RPC, plus events |
| [Schema Design](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/schema.md) | writing the `.proto` that is your contract |
| [Events](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/events.md) | publish/subscribe and wildcard topics |
| [Streaming](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/streaming.md) | server streaming and cancellation |
| [Error Handling](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/error-handling.md) | retriable vs terminal, the retry ladder, the DLQ |
| [Patterns](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/patterns.md) | concurrency, retries, service-to-service calls, deployment |
| [Testing](https://github.com/ArielLaub/protobus-py/blob/main/docs/guide/testing.md) | unit, integration and end-to-end |

| Understand it | |
|---|---|
| [Architecture](https://github.com/ArielLaub/protobus-py/blob/main/docs/concepts/architecture.md) | what a service creates in the broker, and why |
| [Message Flow](https://github.com/ArielLaub/protobus-py/blob/main/docs/concepts/message-flow.md) | the wire format and the round trip |
| [Delivery Guarantees](https://github.com/ArielLaub/protobus-py/blob/main/docs/concepts/delivery-guarantees.md) | acks, confirms, duplicates, the parked caller |

| Look it up | |
|---|---|
| [Configuration](https://github.com/ArielLaub/protobus-py/blob/main/docs/reference/configuration.md) | every environment variable and its default |
| [API reference](https://github.com/ArielLaub/protobus-py/tree/main/docs/reference/api) | Context, MessageService, RunnableService, ServiceProxy |
| [Errors](https://github.com/ArielLaub/protobus-py/blob/main/docs/reference/errors.md) | every exported error class and when it is raised |
| [Custom Types](https://github.com/ArielLaub/protobus-py/blob/main/docs/reference/custom-types.md) | extending the type system |
| [CLI](https://github.com/ArielLaub/protobus-py/blob/main/docs/reference/cli.md) | `generate`, `generate:service`, `init` |

| Run it | |
|---|---|
| [Troubleshooting](https://github.com/ArielLaub/protobus-py/blob/main/docs/operations/troubleshooting.md) | symptom, cause, fix |
| [Security](https://github.com/ArielLaub/protobus-py/blob/main/docs/operations/security.md) | what `actor` does and does not prove |
| [Logging](https://github.com/ArielLaub/protobus-py/blob/main/docs/operations/logging.md) | levels, structured records, your own sink |
| [Queue Migration](https://github.com/ArielLaub/protobus-py/blob/main/docs/operations/queue-migration.md) | changing settings on live queues |
| [Known Issues](https://github.com/ArielLaub/protobus-py/blob/main/docs/operations/known-issues.md) | current limitations |
| [Migration Guide](https://github.com/ArielLaub/protobus-py/blob/main/docs/migration.md) | upgrading from 1.x to 2.0 |

---

## Examples

| Example | Shows |
|---|---|
| [`sample/combatGame`](https://github.com/ArielLaub/protobus-py/tree/main/sample/combatGame) | Six instances of one service playing a game over RPC and events, with graceful shutdown |
| [`sample/tokenStream`](https://github.com/ArielLaub/protobus-py/tree/main/sample/tokenStream) | Streaming tokens like a language model, with cancellation stopping the producer |

```bash
git clone https://github.com/ArielLaub/protobus-py && cd protobus-py
python -m venv venv && venv/bin/pip install -e ".[dev]"
docker compose up -d
PYTHON=$PWD/venv/bin/python scripts/run-combat-sample.sh   # asserts that exactly one player survived
```

---

## License

MIT. See [LICENSE](https://github.com/ArielLaub/protobus-py/blob/main/LICENSE).
