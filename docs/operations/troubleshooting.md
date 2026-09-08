# Troubleshooting

> Symptom, cause, fix. Find the error text you are staring at.

**Read this if** something is failing and you want the shortest path to why.

| | |
|---|---|
| **Prerequisites** | [Getting Started](../guide/getting-started.md) |
| **Next** | [Known Issues](./known-issues.md) · [Configuration](../reference/configuration.md) |
| **Source** | [`protobus/connection.py`](../../protobus/connection.py) · [`protobus/message_service.py`](../../protobus/message_service.py) · [`protobus/logger.py`](../../protobus/logger.py) |

**On this page** — [Find your error](#find-your-error) · [Starting up](#starting-up) · [Connection](#connection) · [Schema](#schema) · [RPC](#rpc) · [Events](#events) · [Performance](#performance) · [Turn on debug logging](#turn-on-debug-logging) · [Look at the broker](#look-at-the-broker)

---

## Find your error

| You see | Section |
|---|---|
| The script prints its answer and never exits | [A client that never exits](#a-client-that-never-exits) |
| `MissingProto: no service in the schema matches '...'` | [MissingProto](#missingproto) |
| `ProtoParseError: unknown type 'uuid'` (or any custom type name) | [Unknown type in a schema](#unknown-type-in-a-schema) |
| `AMQPConnectionError: ... Connect call failed` | [Connection refused](#connection-refused) |
| `ACCESS_REFUSED - Login was refused` | [Authentication failed](#authentication-failed) |
| `UnroutableError` | [Nobody is serving that queue](#unroutableerror-nobody-is-serving-that-queue) |
| `RpcTimeoutError` / the call hangs then raises | [Request timeout](#request-timeout) |
| `AttributeError: 'ServiceProxy' object has no attribute 'myMethod'` | [Method is not there](#method-is-not-there) |
| `InvalidRequestError: failed parsing message` | [The request did not encode](#the-request-did-not-encode) |
| `PublishConfirmTimeoutError` / `PublishNackedError` | [Errors reference](../reference/errors.md) |
| Debug logging produces nothing | [Turn on debug logging](#turn-on-debug-logging) |
| Events publish fine but no handler runs | [Events not received](#events-not-received) |
| A handler that worked in 1.x takes a different branch | [Migration](../migration.md#the-dangerous-one-proto3-zero-values) |

---

## Starting up

### A client that never exits

**Symptom.** A short script calls a service, prints the right answer, and then
sits there forever.

**Cause.** The AMQP socket and its heartbeat are still registered on the event
loop, so `asyncio.run()` never returns. Nothing closes them for you.

**Fix.**

```python
import asyncio

from protobus import Context, ServiceProxy


async def main() -> None:
    context = Context()
    await context.init("amqp://localhost", ["./proto"])

    calc = ServiceProxy(context, "Calculator.Math")
    await calc.init()
    print(await calc.add({"a": 5, "b": 3}))

    await context.close()   # this is the missing line


asyncio.run(main())
```

> [!NOTE]
> A long-running **server** does not need this. `RunnableService.start` installs
> SIGINT/SIGTERM handlers that stop consuming, drain in-flight work, and
> disconnect. See [RunnableService](../reference/api/runnable-service.md).

### MissingProto

**Symptom**

```
MissingProto: no service in the schema matches 'Calculator.Subscriber' or any
prefix of it; the .proto must declare the service this class serves
```

**Cause.** Protobus resolves a class's contract by looking its `service_name` up
in the loaded schema, trimming segments from the right until one matches
(`_resolve_contract` in [`protobus/message_service.py`](../../protobus/message_service.py)).
Nothing matched.

**Fixes, in order of likelihood.**

1. **The service block is missing from the `.proto`.** This catches everyone once,
   because it applies **even to a service that only subscribes to events and
   implements no RPCs**. An empty block is enough:

   ```protobuf
   service Subscriber {
   }
   ```

2. **The schema was never loaded.** `context.init(url, paths)` scans each path
   recursively for `.proto` files. Check the path is the one you think it is —
   a relative path is relative to the process working directory.

3. **The name does not match.** It is `package` + `.` + `service`:
   `package Calculator` with `service Math` is `Calculator.Math`.

> [!TIP]
> Trailing segments beyond the contract are allowed and are trimmed away.
> `Combat.Player.player6` resolves against `service Player` in package `Combat`,
> which is how you give each instance of a service its own queue.

A different `MissingProto`, `missing_proto_source`, means the schema was not
loaded by `Context.init()` *and* the file named by `proto_file_name` does not
exist. `RunnableService` derives that name as `<PROTO_PATH>/<Package>.proto`,
`PROTO_PATH` defaulting to `./proto` **relative to the working directory**.
Pass the proto directory to `Context.init()`, or set `proto_file_name` to an
absolute path built from `__file__`.

### Unknown type in a schema

**Symptom**

```
ProtoParseError: unknown type 'uuid' (not a scalar, not declared in this file
or an imported one, and not a registered custom type: bigint, timestamp)
(accounts.proto, line 5)
```

**Cause.** One of three things: `uuid` is a custom type that was not registered
before the schema was parsed; it is a message declared in another `.proto`
that was not under any of the paths passed to `Context.init()`; or it is a
typo. The message lists the custom types that *are* registered, which settles
the first case at a glance.

**Fix.** Register the type on the factory **before** `context.init()` — `init()`
parses your protos:

```python
context = Context()
context.factory.register_type(UuidType)     # first
await context.init("amqp://localhost", ["./proto"])
```

See [Custom Types](../reference/custom-types.md).

---

## Connection

### Connection refused

**Symptom**

```
aiormq.exceptions.AMQPConnectionError: Multiple exceptions: [Errno 61] Connect call failed ('127.0.0.1', 5672)
```

**Check, in order.**

```bash
docker ps | grep rabbit          # is a broker running at all
rabbitmqctl status               # or, if it is installed natively
curl -u guest:guest localhost:15672/api/overview   # management API answering
```

The default port is 5672 (15672 is the management UI, not the AMQP port — a
surprisingly common mix-up). If the broker is on another host, check the firewall
allows 5672.

The first connection is **not** retried: `Context.init()` raises straight
away. Reconnection only takes over once a connection has been established and
then lost. A service that has to wait for a broker that is still starting loops
on `init()` itself, or is started by something that restarts it.

### Authentication failed

**Symptom**

```
aiormq.exceptions.ProbableAuthenticationError: ACCESS_REFUSED - Login was refused using authentication mechanism PLAIN
```

**Causes.**

- Wrong credentials in the URL: `amqp://user:password@host:5672/`.
- Wrong virtual host. The path segment after the port is the vhost:
  `amqp://user:pass@host:5672/` is the default `/`, and
  `amqp://user:pass@host:5672/my-vhost` is a different one. An empty path and a
  named path are not the same broker namespace.
- The user exists but has no permissions on that vhost:

  ```bash
  rabbitmqctl add_user myuser mypassword
  rabbitmqctl set_permissions -p / myuser ".*" ".*" ".*"
  ```

> [!CAUTION]
> RabbitMQ refuses `guest` over a non-loopback connection by default. A
> connection string that works on your laptop and fails from a container is
> usually this, not a typo.

### Connection drops

Protobus reconnects automatically. The relevant knobs and events:

```python
from protobus import Context, ContextOptions, ReconnectionOptions

context = Context()
await context.init("amqp://localhost", ["./proto"], ContextOptions(
    reconnection=ReconnectionOptions(
        max_retries=0,        # 0 = keep trying forever
        max_delay_ms=30000,
    ),
))

context.connection.on("disconnected", lambda: print("connection lost"))
context.connection.on("reconnected", lambda: print("connection restored"))
```

If reconnection never succeeds, the cause is almost always outside protobus:
the broker is gone, the credentials were rotated, or a network policy changed.
Touch `AMQP_HEARTBEAT_SECONDS` only after ruling those out — see
[Configuration](../reference/configuration.md#heartbeats).

At the end of a script you may see `connection error` or `closed unexpectedly`
lines *after* your `main()` returned: that is a context that was never
`close()`d being torn down by the loop. Close it.

---

## Schema

### Proto file not found

**Symptom**

```
MissingProto: missing_proto_source
```

`proto_file_name` is resolved relative to the process's working directory
unless it is absolute, and the working directory is not where your source file
lives:

```python
proto_file_name = "./service.proto"                                          # fragile
proto_file_name = os.path.join(os.path.dirname(__file__), "service.proto")   # correct
```

`RunnableService` derives the name by convention from `service_name`
(`Calculator.Math` → `./proto/Calculator.proto`) and never reads the file when
`Context.init()` already loaded the schema, so passing the proto directory to
`init()` avoids the question entirely.

### A type another file declares is unknown

**Symptom**

```
ProtoParseError: unknown type 'common.Money' (...)
```

Files are loaded in dependency order however they are found, but only from the
locations you passed. `common/money.proto` has to be under one of them; an
`import` line alone does not fetch it.

---

## RPC

### UnroutableError: nobody is serving that queue

**Symptom.** A call raises immediately with `UnroutableError` rather than
hanging.

**Cause.** RPC requests are published with AMQP's `mandatory` flag, so the broker
returns a message that reaches no queue instead of dropping it. Nothing is bound
for that service name.

**Check.**

```bash
rabbitmqctl list_queues name consumers | grep MyPackage.MyService
```

Zero consumers means the service is not running. No queue at all means it has
never run against this broker, or the name is misspelled.

> [!TIP]
> This failing fast is the point. In 1.x the same mistake produced a caller
> that waited out the full RPC timeout. See
> [Migration](../migration.md) and [Errors](../reference/errors.md).

### Request timeout

**Symptom.** `RpcTimeoutError` after a long wait.

**There are two separate clocks, and raising the wrong one does nothing.**

| Setting | Whose | Default | What it bounds |
|---|---|---|---|
| `RPC_CALL_TIMEOUT_MS` | the **caller's** | `600000` (10 min) | how long `proxy.method()` waits for a reply |
| `MESSAGE_PROCESSING_TIMEOUT` | the **server's** | `600000` (10 min) | how long a handler may run before the delivery is abandoned |

If your caller gives up, raise `RPC_CALL_TIMEOUT_MS` (or pass `timeout_ms` on the
call). If your handler is being cut off mid-work, raise
`MESSAGE_PROCESSING_TIMEOUT` on the service, or its `processing_timeout_ms`.

**Before raising either**, check that the callee is not simply failing and
retrying: a request that keeps raising climbs the retry ladder without
publishing a reply, so the caller sees a long silence rather than an error. See
[Delivery Guarantees](../concepts/delivery-guarantees.md).

### Method is not there

**Symptom**

```
AttributeError: 'ServiceProxy' object has no attribute 'myMethod'
```

**Cause.** `ServiceProxy` builds its methods from the schema during `init()`.
Before that it has none.

```python
proxy = ServiceProxy(context, "MyPackage.MyService")
await proxy.init()          # this is what installs the methods
```

If `init()` did run, the method name does not match the `rpc` name in the
`.proto` exactly — including case. `createOrder` in the schema is
`proxy.createOrder`, not `proxy.create_order`.

> [!NOTE]
> A type-checker will not catch either mistake: the stubs exist only at
> runtime. Annotate the proxy with the `Protocol` that `protobus generate`
> writes so at least the argument and return types are checked. See
> [ServiceProxy](../reference/api/service-proxy.md).

### The request did not encode

**Symptom**

```
InvalidRequestError: failed parsing message
```

with a log line just above it naming the field:
`failed building message 'Calc.AddRequest': field 'a' expects an integer, got a str that is not one`.

**Cause.** A value in the request dict does not fit its field: a string where
the schema says `int32`, an enum name the enum does not declare, a `list` for a
scalar. The exception's `__cause__` is a `FieldTypeError` or `FieldValueError`
naming the field and the value's type; the value itself is deliberately kept
out of every protobus exception message and log line. (A codec's own error —
the reason a `bigint` was refused — is chained one level further down, in the
caller's process only.)

---

## Events

### Events not received

`publish_event()` returns but no handler runs.

1. **Subscribe before you publish.** An event published with no matching binding
   is discarded by the exchange; there is no replay.

2. **The topic pattern does not match.** `*` matches exactly **one** segment;
   `#` matches zero or more. Publishing to `ORDERS.US.SHIPPED`:

   | Pattern | Matches |
   |---|---|
   | `ORDERS.US.*` | yes |
   | `ORDERS.*.SHIPPED` | yes |
   | `ORDERS.#` | yes |
   | `ORDERS.EU.*` | no |
   | `ORDERS.*` | no — `*` is one segment, and there are two after `ORDERS` |

   The last row catches people. See [Events](../guide/events.md).

3. **The handler has the wrong arity.** A two-parameter handler receives
   `(event, topic)`, not `(event, type)` — a handler that switches on its
   second argument expecting a type name never matches. Declare all three:
   `(event, event_type, topic)`.

4. **The exchange is not there.**

   ```bash
   rabbitmqctl list_exchanges | grep proto.bus
   ```

### The same event is handled twice

**Causes.** Subscribing more than once (subscribe in `init()`, once), or a
process that died mid-handler — an unacknowledged event delivery is redelivered.

Delivery is **at-least-once**, so a handler that must not run twice has to be
idempotent. Key on something stable in the event, not on arrival order:

```python
processed: set[str] = set()


async def handle_event(event: dict) -> None:
    if event["id"] in processed:
        return
    processed.add(event["id"])
    # ... do the work
```

An in-process `set` is fine for a demo and wrong for anything with more than one
replica or a restart. Use a store the replicas share.

---

## Performance

### High memory use

**Unbounded in-flight messages.** `max_concurrent` is the consumer prefetch: it
bounds how many unacknowledged messages the broker will push into this process.
It defaults to **1**.

```python
service = MyService(context, max_concurrent=10)
```

**Large payloads.** Protobuf messages are held in memory whole. Chunk them, or use
[streaming](../guide/streaming.md).

**Buffered streams.** A consumer slower than its producer buffers chunks in the
dispatcher, up to `STREAM_MAX_BUFFERED_*` — and fails loudly with
`StreamBackpressureError` rather than growing further.

### Slow processing

Scale by running **more processes**, not by packing more services into one. One
asyncio loop is one core, so co-locating services buys no parallelism — it only
couples their failure domains and their deploys. Each replica competes for the
same durable queue, which is the whole design; see
[Architecture](../concepts/architecture.md).

Within one process, raise `max_concurrent` so a replica works on several
messages while others wait on I/O — and make sure nothing in a handler blocks
the loop. A synchronous database driver or a CPU-bound loop stalls every other
in-flight message and the heartbeat with it; wrap it in
`await asyncio.to_thread(...)`.

> [!NOTE]
> `ServiceCluster` still exists in protobus-py 2.0 for hosting several
> services in one process, but it is not a concurrency tool — see above.

---

## Turn on debug logging

**Installing a logger is not enough.** Debug is off by default, and
`Logger.debug` is filtered against the level *before* it reaches your sink
([`protobus/logger.py`](../../protobus/logger.py)) — so a custom logger with a
`debug` method receives nothing and you conclude protobus emits no debug output.

You need **both** a sink and a level:

```python
from protobus import LogLevel, set_log_level, set_logger


class DebugLogger:
    def debug(self, message): print("[DEBUG]", message)
    def info(self, message): print("[INFO]", message)
    def warn(self, message): print("[WARN]", message)
    def error(self, message): print("[ERROR]", message)


set_logger(DebugLogger())
set_log_level(LogLevel.Debug)   # without this line, debug output is discarded
```

Or set it from the environment, which needs no code at all:

```bash
LOG_LEVEL=debug python server.py
```

`LOG_LEVEL` accepts `debug`, `info` (the default), `warn`, `error` and `silent`.

> [!CAUTION]
> Debug logging can include message payloads once a diagnostics serializer is
> installed. That is why none is installed by default. Turn it on deliberately,
> and see [Security](./security.md) before doing it in production.

For machine-readable records and your own sink, see [Logging](./logging.md).

---

## Look at the broker

The management UI at <http://localhost:15672> (`guest` / `guest`) answers most
questions faster than any log line. From the command line:

<details>
<summary>Useful <code>rabbitmqctl</code> commands</summary>

```bash
# Which queues exist, how deep, and how many consumers
rabbitmqctl list_queues name messages consumers

# Just this service — should show 4: the queue, .Events, .Retry, .DLQ
rabbitmqctl list_queues name messages | grep '^MyPackage.MyService'

# The exchanges protobus declares
rabbitmqctl list_exchanges | grep proto.bus

# What is bound to the events exchange
rabbitmqctl list_bindings | grep proto.bus.events

# Anything in the dead-letter queue is a message that exhausted its retries
rabbitmqctl list_queues name messages | grep '\.DLQ'

# Empty a queue. Destructive.
rabbitmqctl purge_queue MyPackage.MyService.Events
```

</details>

A message in `<Service>.DLQ` carries headers saying why it got there —
`x-retry-count`, `x-last-error`, `x-first-failure-time` and others. They are the
fastest way to diagnose a failing handler in production; see
[Delivery Guarantees](../concepts/delivery-guarantees.md).

---

<div align="center">

**[← Architecture](../concepts/architecture.md)** · **[Docs index](../README.md)** · **[Known Issues →](./known-issues.md)**

</div>
