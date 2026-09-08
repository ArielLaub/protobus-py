# ProtoBus for Python

**RabbitMQ-native microservices for Python, with Protocol Buffers on the wire.**

[![PyPI](https://img.shields.io/pypi/v/protobus.svg?logo=pypi)](https://pypi.org/project/protobus/)
[![python](https://img.shields.io/badge/python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![RabbitMQ](https://img.shields.io/badge/RabbitMQ-%E2%89%A53.8-FF6600?logo=rabbitmq&logoColor=white)](https://www.rabbitmq.com)
[![CI](https://github.com/ArielLaub/protobus-py/actions/workflows/ci.yml/badge.svg)](https://github.com/ArielLaub/protobus-py/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Define a service in a `.proto` file, implement it as a class, and call it from
anywhere on the bus as if it were local. ProtoBus turns each service into **one
durable RabbitMQ queue with N processes competing for it** — so load balancing,
failover, backpressure, retries and dead-lettering are the broker's, not
Python's.

This is the Python port of [protobus](https://github.com/ArielLaub/protobus)
(TypeScript). The two are **wire-compatible**: a Python service serves
TypeScript callers and vice versa, streaming, events, custom types and error
codes included. Every commit runs a cross-language suite in both directions
against a live broker and a pinned TypeScript revision (2.4.0) to keep it
that way; the behavioural differences that remain are listed in
[Known Issues](docs/operations/known-issues.md#differences-from-the-typescript-port).

---

## Install

```bash
pip install protobus
```

You also need a RabbitMQ 3.8+ broker:

```bash
docker run -d --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3-management-alpine
```

No `protoc` is needed: `.proto` files are parsed at runtime.

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

Package plus service name is the service's name on the bus: `Calculator.Math`.

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
disconnects — in that order.

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

Full walkthrough, including events and the project layout:
**[Getting Started](docs/guide/getting-started.md)**.

---

## Why ProtoBus

### RabbitMQ only, on purpose

ProtoBus is built for one broker, so the things a broker is good at stay in the
broker instead of being reimplemented above it:

| Concern | Where it lives |
|---|---|
| Load balancing | competing consumers on one queue |
| Routing | topic exchange bindings (`REQUEST.<Service>.*`) |
| Redelivery on consumer loss | late ack — an unacked delivery returns to the queue |
| Retry delay | the retry queue's `x-message-ttl`, drained by DLX |
| Persistence | durable queues, persistent messages |
| Dead letters | a real `<Service>.DLQ` |
| Priority | native queue priorities |

A request goes publisher → exchange → queue → consumer. Nothing tracks live
instances, so nothing holds a stale one, and a consumer that dies mid-request
leaves its delivery unacked for the next consumer to take.

The cost of this is written down rather than glossed over — read
[Delivery Guarantees](docs/concepts/delivery-guarantees.md) before you rely on
any of it. If you may need to swap RabbitMQ for another broker, use a
transport-agnostic framework instead; that is a real feature and protobus does
not have it.

### Protocol Buffers, not JSON

- **Usually smaller on the wire** — field numbers and packed integers rather
  than text; how much smaller depends on the payload.
- **Contract-first** — a `.proto` file is the interface between teams, and
  generated typing (`protobus generate`) tells you when the two drift apart.
- **Versioning by field number** — adding a field does not break an old peer.

### Two runtime dependencies

[`aiormq`](https://github.com/mosquito/aiormq) for AMQP and
[`protobuf`](https://pypi.org/project/protobuf/) for the wire. The messaging
behaviour that would be hardest to reimplement — queueing, consumer
distribution, retry delays, dead-lettering — is RabbitMQ's.

---

## Streaming, cancellation, events, priority

```python
# A server-streaming rpc (`returns (stream Token)`) is an async generator.
async def generate(self, request, actor, correlation_id, context):
    for i, word in enumerate(words):
        if context.signal.aborted:      # the caller stopped listening
            return
        yield {"index": i, "text": word}

# The client consumes it with `async for`; closing the stream tells the
# server to stop producing.
async with assistant.generate({"prompt": "..."}) as stream:
    async for token in stream:
        print(token["text"], end="")
        if enough:
            break
```

Events are published to a topic exchange and matched with `*` and `#`
wildcards; an opt-in retry ladder gives event handlers the same retry/DLQ
treatment requests get. Priority queues let a control message overtake a bulk
backlog. See [Streaming](docs/guide/streaming.md),
[Events](docs/guide/events.md) and [Priority](docs/guide/priority.md).

---

## Custom types

Protobuf's scalars do not cover everything. Register a custom type and it becomes
usable as a field type in your schemas, encoded and decoded transparently:

```python
from protobus import Context, CustomType

UuidType = CustomType(
    name="uuid",            # how it is written in the .proto
    wire_type="string",     # how it travels
    encode=lambda value: str(value),
    decode=lambda data: data,
    py_type="str",          # what generated typing calls it
)

context = Context()
# Register before init(): init() parses your .proto files, and a schema
# using `uuid` cannot be parsed until the type exists.
context.factory.register_type(UuidType)
await context.init("amqp://localhost", ["./proto"])
```

```protobuf
syntax = "proto3";
package Accounts;

message Account {
    uuid id = 1;
}
```

`BigIntType` (an unsigned 256-bit integer, decoded to `int`) and
`TimestampType` (milliseconds since the epoch, decoded to a timezone-aware
`datetime`) ship with the library and are already registered. They are the
same wire format the TypeScript port uses.
**[Custom Types](docs/reference/custom-types.md)**.

---

## CLI

```bash
protobus generate               # .proto -> Python typing (TypedDict / Protocol)
protobus generate:service Name  # a runnable service stub from Name.proto
protobus init                   # print project setup instructions
```

Configured from `pyproject.toml`:

```toml
[tool.protobus]
proto_dir = "./proto"
types_output = "./types/proto.py"
services_dir = "./services"
```

All three keys are optional; the defaults above are what the CLI uses.
**[CLI reference](docs/reference/cli.md)**.

---

## Documentation

Full index: **[docs/](docs/README.md)**

| Start | |
|---|---|
| [Getting Started](docs/guide/getting-started.md) | zero to a working RPC, plus events |
| [Schema Design](docs/guide/schema.md) | writing the `.proto` that is your contract |
| [Events](docs/guide/events.md) | publish/subscribe and wildcard topics |
| [Error Handling](docs/guide/error-handling.md) | retriable vs terminal, the retry ladder, the DLQ |
| [Testing](docs/guide/testing.md) | unit, integration and end-to-end |

| Understand it | |
|---|---|
| [Architecture](docs/concepts/architecture.md) | what a service creates in the broker, and why |
| [Message Flow](docs/concepts/message-flow.md) | the wire format and the round trip |
| [Delivery Guarantees](docs/concepts/delivery-guarantees.md) | acks, confirms, duplicates, the parked caller |

| Look it up | |
|---|---|
| [Configuration](docs/reference/configuration.md) | every environment variable and its default |
| [API reference](docs/reference/api) | Context, MessageService, RunnableService, ServiceProxy |
| [Errors](docs/reference/errors.md) | every exported error class and when it is raised |
| [Custom Types](docs/reference/custom-types.md) | extending the type system |

| Run it | |
|---|---|
| [Troubleshooting](docs/operations/troubleshooting.md) | symptom, cause, fix |
| [Security](docs/operations/security.md) | what `actor` does and does not prove |
| [Logging](docs/operations/logging.md) | levels, structured records, your own sink |
| [Queue Migration](docs/operations/queue-migration.md) | changing settings on live queues |
| [Known Issues](docs/operations/known-issues.md) | current limitations |
| [Migration Guide](docs/migration.md) | upgrading from 1.x to 2.0 |

---

## See a real system in a minute

```bash
git clone https://github.com/ArielLaub/protobus-py && cd protobus-py
python -m venv venv && venv/bin/pip install -e ".[dev]"
docker compose up -d
PYTHON=$PWD/venv/bin/python scripts/run-combat-sample.sh
```

Six services fight a battle royale over the bus — RPC, published events and
graceful shutdown in one run — and the script asserts exactly one player
survived. The source is [`sample/combatGame`](sample/combatGame). A second
sample, [`sample/tokenStream`](sample/tokenStream), streams tokens like an LLM
and shows cancellation actually stopping the producer.

---

## Requirements

- Python 3.10+ (CI runs 3.10 – 3.13)
- RabbitMQ 3.8+

## Development

```bash
python -m pytest tests/unit                 # unit suite, no broker needed
docker compose up -d
python -m pytest tests/integration          # against RabbitMQ (management API at :15672 for the recovery tests)
PYTHON=$PWD/venv/bin/python scripts/run-combat-sample.sh
```

The cross-language suite (`tests/integration/test_cross_language.py`) expects
the TypeScript checkout built beside this repo, or `PROTOBUS_TS` pointing at
it; the mirror test in that repo drives a Python server from a TypeScript
client.

## License

MIT. See [LICENSE](LICENSE).
