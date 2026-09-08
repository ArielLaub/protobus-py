<div align="center">

# ProtoBus for Python — Documentation

**RabbitMQ-native microservices with Protocol Buffers.**

[![PyPI](https://img.shields.io/pypi/v/protobus.svg?logo=pypi)](https://pypi.org/project/protobus/)
[![python](https://img.shields.io/badge/python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![RabbitMQ](https://img.shields.io/badge/RabbitMQ-%E2%89%A53.8-FF6600?logo=rabbitmq&logoColor=white)](https://www.rabbitmq.com)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](../LICENSE)

</div>

---

## Start here

| I want to… | Go to | Time |
|---|---|---|
| **See it work before I read anything** | [Run the sample](#run-the-sample-in-60-seconds) | 1 min |
| **Decide whether to adopt it** | [Why ProtoBus](./why-protobus.md) → [Architecture](./concepts/architecture.md) | 15 min |
| **Build my first service** | [Getting Started](./guide/getting-started.md) → [CLI](./reference/cli.md) → [Configuration](./reference/configuration.md) | 30 min |
| **Understand why it is reliable** | [Delivery Guarantees](./concepts/delivery-guarantees.md) | 15 min |
| **Look something up** | [Reference](#reference) · [Troubleshooting](./operations/troubleshooting.md) | — |
| **Upgrade from 1.x** | [Migration](./migration.md) · [CHANGELOG](../CHANGELOG.md) | — |

### Run the sample in 60 seconds

```bash
git clone https://github.com/ArielLaub/protobus-py && cd protobus-py
python -m venv venv && venv/bin/pip install -e ".[dev]"
docker compose up -d
PYTHON=$PWD/venv/bin/python scripts/run-combat-sample.sh
```

Six services fight a battle royale over the bus — RPC, pub/sub events, and clean
shutdown in one run — and the script asserts exactly one player survived. Open
<http://localhost:15672> (`guest`/`guest`) to watch the queues while it runs.
The source is [`sample/combatGame`](../sample/combatGame).

---

## Guide

Read in order. Each page assumes the ones above it.

| # | Page | What it gives you |
|---|---|---|
| 1 | **[Getting Started](./guide/getting-started.md)** | A running service, a client that calls it, and an event that arrives |
| 2 | **[Schema Design](./guide/schema.md)** | Writing the `.proto` that is your contract |
| 3 | **[Events](./guide/events.md)** | Publish/subscribe, topics and wildcards |
| 4 | **[Error Handling](./guide/error-handling.md)** | Retriable vs terminal, the retry ladder, the DLQ |
| 5 | **[Testing](./guide/testing.md)** | Unit, integration and end-to-end, without a broker where possible |
| 6 | **[Patterns](./guide/patterns.md)** | Worked examples assembled from all of the above |
| — | [Streaming RPC](./guide/streaming.md) | `returns (stream Chunk)`, backpressure, cancellation |
| — | [Message Priority](./guide/priority.md) | Letting control messages overtake a bulk backlog |

---

## Concepts

How it works. Read once, refer back.

| Page | |
|---|---|
| **[Architecture](./concepts/architecture.md)** | What a service creates in the broker, and why the design is one queue with N consumers |
| **[Message Flow](./concepts/message-flow.md)** | The wire format and one round trip in detail |
| **[Delivery Guarantees](./concepts/delivery-guarantees.md)** | Acks, publish confirms, the retry ladder's headers, duplicates, and the parked caller |

---

## Reference

| Page | |
|---|---|
| **[Configuration](./reference/configuration.md)** | Every environment variable and its real default |
| **[CLI](./reference/cli.md)** | `generate`, `generate:service`, and what they actually emit |
| **[Errors](./reference/errors.md)** | Every exported error class and the condition that raises it |
| **[Custom Types](./reference/custom-types.md)** | `BigIntType`, `TimestampType`, and registering your own |

**API**

| Class | Use it to | |
|---|---|---|
| [Context](./reference/api/context.md) | hold the connection and the proto registry — one per process | |
| [MessageService](./reference/api/message-service.md) | implement a service | base class |
| [RunnableService](./reference/api/runnable-service.md) | implement a service that owns its process | preferred |
| [ServiceProxy](./reference/api/service-proxy.md) | call a remote service | |

---

## Operations

Running it in production. None of this is advanced; it is mandatory.

| Page | |
|---|---|
| **[Troubleshooting](./operations/troubleshooting.md)** | Symptom, cause, fix — start from the error text |
| **[Security](./operations/security.md)** | What `actor` does *not* prove, and what leaves the process |
| **[Logging](./operations/logging.md)** | Levels, your own sink, structured records, payload diagnostics |
| **[Queue Migration](./operations/queue-migration.md)** | Changing settings on a live queue without losing messages |
| **[Known Issues](./operations/known-issues.md)** | Current limitations |

---

## How this documentation is kept honest

The [Getting Started](./guide/getting-started.md) tutorial is executed, file by
file, against a real broker by
[`scripts/check-getting-started.sh`](../scripts/check-getting-started.sh), and
the claims a snippet cannot assert about itself (which wildcard matches which
topic, what a zero value decodes to, what an operator reads off a DLQ message)
are pinned in the unit and integration suites — the pages name the test next
to the claim.

If you change a documented behaviour, one of those will tell you.

---

## Other languages

The `.proto` files are the contract and RabbitMQ does the routing, so a port
needs only protobuf and an AMQP client.

| Language | Repo | Status |
|---|---|---|
| TypeScript / Node | [protobus](https://github.com/ArielLaub/protobus) | stable |
| Python | [protobus-py](https://github.com/ArielLaub/protobus-py) (this repository) | stable |
| Go | [protobus-go](https://github.com/ArielLaub/protobus-go) | experimental |

The Python and TypeScript ports are verified wire-compatible in both directions
on every commit ([`tests/integration/test_cross_language.py`](../tests/integration/test_cross_language.py)).
The few behavioural differences are recorded per feature — see
[Known Issues → Differences from the TypeScript port](./operations/known-issues.md#differences-from-the-typescript-port).

---

<div align="center">

Documentation for **protobus-py 2.0.x** · [CHANGELOG](../CHANGELOG.md) · [Report a docs issue](https://github.com/ArielLaub/protobus-py/issues)

</div>
