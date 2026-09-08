# Getting Started

> From an empty directory to an RPC that returns the right answer, and an event that arrives.

**Read this if** you have never run a protobus service.

| | |
|---|---|
| **Prerequisites** | Python 3.10+, Docker (for RabbitMQ), a terminal |
| **Next** | [Architecture](../concepts/architecture.md) — what you just created in the broker |
| **Source** | [`protobus/context.py`](../../protobus/context.py) · [`protobus/message_service.py`](../../protobus/message_service.py) · [`protobus/service_proxy.py`](../../protobus/service_proxy.py) |

**On this page** — [See it work first](#see-it-work-first) · [Set up](#set-up-the-project) · [1. Schema](#1-define-the-schema) · [2. Context](#2-create-the-context) · [3. Service](#3-implement-the-service) · [4. Server](#4-start-the-server) · [5. Client](#5-call-it) · [6. Events](#6-subscribe-to-events) · [Project layout](#project-layout) · [Where next](#where-next)

---

## See it work first

Before writing anything, watch a real system run. This takes about a minute and needs only Docker and Python:

```bash
git clone https://github.com/ArielLaub/protobus-py && cd protobus-py
python -m venv venv && venv/bin/pip install -e ".[dev]"
docker compose up -d
PYTHON=$PWD/venv/bin/python scripts/run-combat-sample.sh
```

Six services fight a battle royale over the bus — RPC calls, published events and
a clean shutdown, all in one run — and the script asserts exactly one player
survived. Open <http://localhost:15672> (`guest` / `guest`) while it runs and you
can watch the queues fill and drain.

That sample is [`sample/combatGame`](../../sample/combatGame), and it is the best
worked example in this repository. Come back to it once the tutorial below makes
sense.

---

## Set up the project

```bash
mkdir calculator && cd calculator
python -m venv venv && source venv/bin/activate
pip install protobus
```

Start a broker:

```bash
docker run -d --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3-management-alpine
```

No `protoc`, no generated `_pb2` modules: protobus parses `.proto` files itself.

---

## 1. Define the schema

The `.proto` file is the contract. Create `proto/Calculator.proto`:

```protobuf
syntax = "proto3";
package Calculator;

message AddRequest {
    int32 a = 1;
    int32 b = 2;
}

message AddResponse {
    int32 result = 1;
}

message CalculationEvent {
    string operation = 1;
    int32 result = 2;
}

service Math {
    rpc add(Calculator.AddRequest) returns(Calculator.AddResponse);
}

// A service that only subscribes to events still needs a service block.
// See "Subscribe to events" below for why.
service Subscriber {
}
```

Conventions worth knowing before you go further:

- **Package + service name is the full service name.** `package Calculator` plus
  `service Math` gives `Calculator.Math`, which is the name that appears on the
  queue, in the routing key, and in every `ServiceProxy` call.
- **Request and response types may be fully qualified** in `rpc` declarations —
  `Calculator.AddRequest` — or relative, `AddRequest`; both resolve.
- **Events are plain messages**, not part of a `service` block.
  `CalculationEvent` above is published by its fully-qualified type name.

> [!NOTE]
> **A subscribe-only service still needs a `service` block.** Protobus resolves a
> class's contract by looking its `service_name` up in the loaded schema
> ([`protobus/message_service.py`](../../protobus/message_service.py),
> `_resolve_contract`), and refuses to start if nothing matches:
>
> ```
> MissingProto: no service in the schema matches 'Calculator.Subscriber' or any
> prefix of it; the .proto must declare the service this class serves
> ```
>
> An empty `service Subscriber {}` is enough.

---

## 2. Create the context

The `Context` owns the AMQP connection and the parsed schemas. **One per
process** — services and proxies share it.

```python
# context.py
import os
from protobus import Context

AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/")
PROTO_PATHS = [os.path.join(os.path.dirname(__file__), "proto")]


async def create_context() -> Context:
    context = Context()
    await context.init(AMQP_URL, PROTO_PATHS)
    return context
```

`init()` scans each path recursively for `.proto` files and parses them (in
dependency order, whatever their `import` lines say), then connects. Pass
directories or files.

---

## 3. Implement the service

A service is a class whose methods are the `rpc`s in the schema.

```python
# calculator_service.py
from protobus import RunnableService


class CalculatorService(RunnableService):
    # Required: the full service name from the proto — package + service.
    service_name = "Calculator.Math"

    # One method per rpc, matching the name in the proto exactly. Every
    # method receives the decoded request, the caller's `actor` string, and
    # the correlation id; declare a fourth parameter to receive the
    # MessageHandlerContext (signal, routing key, message id, redelivered).
    async def add(self, request: dict, actor: str, correlation_id: str) -> dict:
        result = request["a"] + request["b"]

        # Optional: tell anyone who cares that this happened.
        await self.publish_event("Calculator.CalculationEvent", {"operation": "add", "result": result})

        return {"result": result}
```

`service_name` can be a class attribute, as here, or a property; the
TypeScript-style `ServiceName` is accepted too.

> [!TIP]
> `service_name` may carry extra segments beyond the contract. A class named
> `Calculator.Math.worker7` still resolves against `service Math`, because
> `_resolve_contract` trims segments from the right until one matches. That is
> how you run per-instance services with their own queues —
> [`sample/combatGame`](../../sample/combatGame) uses it to give each player its
> own name.

---

## 4. Start the server

```python
# server.py
import asyncio
from context import create_context
from calculator_service import CalculatorService


async def main() -> None:
    context = await create_context()
    # Blocks until SIGINT/SIGTERM. max_concurrent is the number of in-flight
    # messages per process; the default is 1.
    await CalculatorService.start(context, max_concurrent=2)


asyncio.run(main())
```

```bash
python server.py
```

`RunnableService.start` installs SIGINT/SIGTERM handlers and, on shutdown, stops
taking new work, drains in-flight messages, runs your `cleanup()` hook and
disconnects — in that order. A startup failure runs the same sequence and
re-raises, so an orchestrator can tell a crash-on-boot from a clean stop.
`launch()` does the same setup but returns the running service instead of
blocking, for a process that owns its own loop.

> [!NOTE]
> `RunnableService` derives `proto_file_name` from `service_name` by convention —
> `Calculator.Math` → `<PROTO_PATH>/Calculator.proto`, `PROTO_PATH` defaulting to
> `./proto`. When `Context.init()` already loaded that directory the file is
> never read; the schema is found by name. Use plain
> [`MessageService`](../reference/api/message-service.md) when something else
> owns the process lifecycle.

---

## 5. Call it

```python
# client.py
import asyncio
from protobus import ServiceProxy
from context import create_context


async def main() -> None:
    context = await create_context()

    # ServiceProxy installs one method per rpc in the schema at init().
    # `protobus generate` writes a typing.Protocol for it if you want a
    # type-checker to know the shapes.
    calculator = ServiceProxy(context, "Calculator.Math")
    await calculator.init()

    response = await calculator.add({"a": 5, "b": 3})
    print(f"5 + 3 = {response['result']}")

    # Close the context, or the open AMQP socket keeps the loop alive.
    await context.close()


asyncio.run(main())
```

```bash
$ python client.py
5 + 3 = 8
```

Every proxy method takes the request, then optionally `actor`, `rpc`,
`timeout_ms` and a `CallOptions`. `actor` is a free string that reaches the
service as its second argument — see [Security](../operations/security.md) for
what it does *not* prove.

---

## 6. Subscribe to events

The `add` handler published a `Calculator.CalculationEvent`. Anything on the bus
can receive it, including a service that implements no RPCs at all.

```python
# event_subscriber.py
import asyncio
from protobus import RunnableService
from context import create_context


class EventSubscriber(RunnableService):
    service_name = "Calculator.Subscriber"


async def main() -> None:
    context = await create_context()
    subscriber = await EventSubscriber.launch(context)

    async def on_calculation(event: dict, event_type: str, topic: str) -> None:
        print(f"Received event: {event['operation']} = {event['result']}")

    await subscriber.subscribe_event("Calculator.CalculationEvent", on_calculation)
    print("Listening for events...")
    await subscriber.run()   # until Ctrl-C


asyncio.run(main())
```

Run it in a third terminal, then run the client again:

```bash
$ python event_subscriber.py
Listening for events...
Received event: add = 8
```

An event handler receives `(event, type, topic)`: the decoded payload, the
event's fully-qualified type, and the routing key it was published on. A
handler declaring fewer parameters is called with what it declares.

`Calculator.Subscriber` needs the empty `service Subscriber {}` block added in
[step 1](#1-define-the-schema). Without it, `init()` raises `MissingProto`.

For wildcard topics, competing subscribers, retry, and what a subscription costs
in the broker, see [Events](./events.md).

---

## Project layout

```
calculator/
├── proto/
│   └── Calculator.proto
├── calculator_service.py
├── context.py
├── server.py
├── client.py
└── event_subscriber.py
```

Run each service as **its own process**. A single asyncio loop is one core, so
packing several services into one process buys no parallelism — it only
couples their failure domains and their deploys. Scale by running more
processes; use `max_concurrent` to control how many messages one process
handles at a time.

---

## Generate the typing instead of hand-writing it

```bash
protobus generate                    # .proto -> Python typing
protobus generate:service Calculator # a runnable service stub
```

`generate` writes a `TypedDict` per message, a `Literal` per enum and a
`Protocol` per service whose method signatures are exactly what `ServiceProxy`
installs, so a schema change becomes a type-checker error at every call site.
See [CLI](../reference/cli.md).

---

## Where next

| You want to | Read |
|---|---|
| Understand the queues that just appeared | [Architecture](../concepts/architecture.md) |
| Know what happens when a handler raises | [Error Handling](./error-handling.md) · [Delivery Guarantees](../concepts/delivery-guarantees.md) |
| Tune timeouts, concurrency, reconnection | [Configuration](../reference/configuration.md) |
| Write the schema properly | [Schema Design](./schema.md) |
| Test your service | [Testing](./testing.md) |

---

<div align="center">

**[← Docs index](../README.md)** · **[Architecture →](../concepts/architecture.md)**

</div>
