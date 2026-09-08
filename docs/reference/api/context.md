# Context

> One AMQP connection, one parsed schema registry, and the dispatchers every service and proxy in the process shares.

**Read this if** you are wiring up a process, you reached for a property on `context` and could not find it, or your client script runs correctly and then never exits.

| | |
|---|---|
| **Prerequisites** | [Getting Started](../../guide/getting-started.md) — you have a context that connects |
| **Next** | [MessageService](./message-service.md) · [ServiceProxy](./service-proxy.md) · [Configuration](../configuration.md) |
| **Source** | [`protobus/context.py`](../../../protobus/context.py) · [`protobus/connection.py`](../../../protobus/connection.py) · [`protobus/message_factory.py`](../../../protobus/message_factory.py) |

**On this page** — [The whole surface](#the-whole-surface) · [init](#initamqp_connection_string-proto_locations-options) · [Publishing directly](#publishing-directly) · [Properties](#properties) · [Shutting down](#shutting-down) · [Errors from init](#errors-from-init) · [One per process](#one-context-per-process)

---

## The whole surface

`Context` is small on purpose. This table is all of it — five methods and six properties.

| Member | Signature | Notes |
|---|---|---|
| constructor | `Context(connection=None)` | takes nothing in normal use; configuration happens in `init()`. A `Connection` may be injected for tests |
| `init` | `async (amqp_connection_string, proto_locations=None, options=None, *, proto_dirs=None) -> None` | parses schemas, then connects |
| `close` | `async () -> None` | releases the dispatchers and disconnects; idempotent |
| `publish_message` | `async (content: bytes, routing_key, rpc=True, timeout_ms=None, options: CallOptions=None, priority=None) -> bytes \| None` | raw unary publish; returns the encoded reply |
| `publish_streaming_message` | `(content: bytes, routing_key, idle_timeout_ms=None, options: StreamOptions=None) -> StreamingReply` | raw streaming publish; **not** a coroutine |
| `publish_event` | `async (event_type, content, topic=None) -> None` | `topic` defaults to `EVENT.<event_type>` |
| `factory` | `MessageFactory` | the proto root, encoders and decoders |
| `connection` | `Connection` | channels, reconnection, readiness |
| `message_dispatcher` / `event_dispatcher` | | the dispatchers behind the `publish_*` methods; rarely needed |
| `is_connected` | `bool` | delegates to `connection.is_connected` |
| `is_reconnecting` | `bool` | delegates to `connection.is_reconnecting` |

`IContext` is the `typing.Protocol` a service or proxy actually needs from its context — `connection`, `factory`, `is_connected`, `is_reconnecting` and the three `publish_*` methods. Anything satisfying it can stand in for a `Context` in a test.

---

## `init(amqp_connection_string, proto_locations, options)`

| Parameter | Type | Description |
|---|---|---|
| `amqp_connection_string` | `str` | AMQP connection string. A `?heartbeat=` already in the URL wins; otherwise `AMQP_HEARTBEAT_SECONDS` (default `30`) is appended. |
| `proto_locations` | `str \| list[str]` | Directories or files. A directory is scanned recursively for `*.proto`. `proto_dirs=` is the 1.x keyword for the same argument. |
| `options` | `ContextOptions` | `reconnection`: a `ReconnectionOptions` — `max_retries` (10), `initial_delay_ms` (1000), `max_delay_ms` (30000), `backoff_multiplier` (2.0). See [Configuration → Reconnection](../configuration.md#reconnection-options). |

The order matters, and it is not the order most people assume:

```mermaid
flowchart LR
    A["factory.init(proto_locations)<br/>scan + parse every .proto"] --> B["connection.connect(url)<br/>open the AMQP socket"]
    B --> C["message_dispatcher.init()<br/>publish channel + callback queue"]
    C --> D["event_dispatcher.init()<br/>publish channel"]

    style A fill:#1f6feb,color:#fff,stroke:#1f6feb
```

Schemas are parsed **before** the socket is opened, so a malformed `.proto` or a duplicate type name fails without ever touching the broker. A connection error and a schema error are therefore never ambiguous.

```python
# context.py
import os

from protobus import Context

HERE = os.path.dirname(os.path.abspath(__file__))


async def create_context() -> Context:
    context = Context()

    await context.init(
        os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/"),
        [os.path.join(HERE, "proto"), "/shared/proto"],
    )

    return context
```

> [!NOTE]
> `proto_locations` may be empty. A `MessageService` registers its own schema during `init()` from its `proto_file_name`, so a single-service process does not have to pass a directory at all. Passing one anyway is harmless: re-registering a schema already in the root is a no-op, keyed on both the service name and the schema text ([`protobus/message_factory.py`](../../../protobus/message_factory.py), `parse()`).

### With reconnection options

```python
from protobus import Context, ContextOptions, ReconnectionOptions

context = Context()
await context.init(
    os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/"),
    [os.path.join(HERE, "proto")],
    ContextOptions(reconnection=ReconnectionOptions(
        max_retries=0,           # 0 means keep retrying forever
        initial_delay_ms=500,
        max_delay_ms=10000,
        backoff_multiplier=2.0,
    )),
)
```

---

## Publishing directly

You will normally publish through a [`ServiceProxy`](./service-proxy.md) or a [`MessageService`](./message-service.md), both of which encode and decode for you. The three methods below are the layer underneath.

### `publish_message(content, routing_key, rpc=True, timeout_ms=None, options=None, priority=None)`

Publishes an already-encoded request and returns the encoded reply.

| Parameter | Default | Behaviour |
|---|---|---|
| `rpc` | `True` | Pass `False` for fire-and-forget: returns `None` once the broker confirms. |
| `timeout_ms` | `Config.rpc_call_timeout_ms()` (`RPC_CALL_TIMEOUT_MS`, 600000) | Raises `RpcTimeoutError` if no reply arrives. Ignored when `rpc` is `False`. |
| `options.priority` | unset | AMQP priority 0-255. Only meaningful on a queue declared with `max_priority`. See [Message Priority](../../guide/priority.md). |
| `options.message_id` | a fresh UUID | The id the consumer deduplicates on; pass your own to make a caller-side republish recognisable. |
| `priority` | unset | 1.x keyword; folded into `options`. |

An RPC publish sets `mandatory`, so a request routed to a key nothing is bound to fails immediately with `UnroutableError` rather than after the full timeout. A non-RPC publish does not — an event with no subscribers is normal.

**Building a request by hand.** Encoding uses the *contract* name — what the `.proto` declares — while routing uses whatever the target service bound:

```python
from protobus import Context, RemoteError


async def call_by_hand(context: Context, method: str, data: dict) -> dict:
    buffer = context.factory.build_request(f"Combat.Player.{method}", data, "referee")
    routing_key = f"REQUEST.Combat.Player.player6.{method}"

    response_data = await context.publish_message(buffer, routing_key, True)
    response = context.factory.decode_response(response_data)

    if response.error:
        raise RemoteError(response.error.message, response.error.code, response.error.method)
    return response.result.data
```

You rarely need this: a `ServiceProxy` can be built for an instance name — `ServiceProxy(context, "Combat.Player.player6")` — and does the same thing. [`sample/combatGame/base_player.py`](../../../sample/combatGame/base_player.py) (`call_player_method`) is the worked example. See [MessageService → Instance names](./message-service.md#instance-names-and-the-contract-they-resolve-to) for why the two names differ.

### `publish_streaming_message(content, routing_key, idle_timeout_ms=None, options=None)`

Returns a `StreamingReply` — an async iterator of raw reply bodies that is also an async context manager. It is **not** a coroutine — there is nothing to await before the `async for`. `idle_timeout_ms` defaults to `Config.stream_idle_timeout_ms()` (`STREAM_IDLE_TIMEOUT_MS`, 60000) and bounds the gap *between* chunks, not the stream's total duration. Full protocol in [Streaming](../../guide/streaming.md).

### `publish_event(event_type, content, topic=None)`

| Parameter | Description |
|---|---|
| `event_type` | Fully-qualified message type from the schema, e.g. `Calculator.CalculationEvent` |
| `content` | `dict` matching that message |
| `topic` | Routing key. Omitted or empty, it becomes `EVENT.<event_type>` ([`protobus/event_dispatcher.py`](../../../protobus/event_dispatcher.py)). |

`MessageService.publish_event` forwards to this method, so inside a service the two are interchangeable.

---

## Properties

### `factory`

The `MessageFactory`: the descriptor pool, plus the encode/decode pair for each of the two wire layers. Useful members are `root`, `has_service(name)`, `has_type(name)`, `get_service_method_names(name)`, `is_streaming_method(full_name)`, `register_type(custom_type)`, `parse(text, name)`, `build_request`, `decode_response`, `build_event`, `decode_event` and `export_python(service_names)`.

```python
from context import create_context


async def main() -> None:
    context = await create_context()

    # Was the schema actually loaded? A False here is why a service's init()
    # will raise MissingProto later.
    if not context.factory.has_service("Calculator.Math"):
        raise SystemExit("Calculator.proto was not on any of the proto paths")

    print(context.factory.get_service_method_names("Calculator.Math"))

    await context.close()
```

### `connection`

The AMQP connection wrapper. Four members matter to application code:

| Member | Use |
|---|---|
| `disconnect()` | what `context.close()` calls last |
| `on('reconnecting' \| 'reconnected' \| 'disconnected' \| 'error', fn)` | connection lifecycle events; `fn` is a plain function |
| `when_ready(timeout_ms=None)` | wait for a usable connection; raises `NotReadyError` after `CONNECTION_READY_TIMEOUT_MS` |
| `drain_in_flight(timeout_ms)` | wait for in-flight handlers before tearing down; returns `True` if they finished |

`Context`'s constructor already subscribes to all four events and logs them, so you are adding to that, not replacing it.

### `is_connected` / `is_reconnecting`

Both are plain delegations to the connection. They answer different questions: `is_connected` is `False` during a reconnection, and `is_reconnecting` is what distinguishes "the broker went away and we are working on it" from "this context was never initialised or has been closed".

---

## Shutting down

```python
from context import create_context


async def main() -> None:
    context = await create_context()

    # ... do the work ...

    # Without this the loop never idles: the open AMQP socket and its
    # heartbeat keep asyncio.run() from returning.
    await context.close()


asyncio.run(main())
```

> [!WARNING]
> A short-lived client that never closes its context does not exit. This is the single most common way a documented example goes wrong, and it is silent — the work all succeeds and the script simply never returns to the shell.

`close()` is idempotent and releases the dispatchers (failing any pending RPC futures, closing any in-flight streams) before disconnecting. A long-running server does not need to call it. [`RunnableService.start`](./runnable-service.md#runnableservicestartcontext-service_class-options-post_init-option_kwargs) installs SIGINT/SIGTERM handlers that stop consumers, drain in-flight work, run `cleanup()` and then close the context for you.

---

## Errors from init

| Symptom | Cause | Fix |
|---|---|---|
| `aiormq.exceptions.AMQPConnectionError: … Connect call failed` | no broker at the URL | start it; `docker compose up -d` for the bundled compose file |
| `aiormq.exceptions.ProbableAuthenticationError: ACCESS_REFUSED` | credentials or vhost wrong in the URL | see [AMQP Connection String](../configuration.md#amqp-connection-string) |
| `ProtoParseError: … (file, line N)` | the parser rejected a schema — an unknown type, a duplicate, a syntax error | fix the `.proto`; the message names the file and line |
| `ProtoParseError: unknown type 'X'` for a type another file declares | that file is not under any of the `proto_locations` | pass its directory too |
| `ReconnectionError` | the connection dropped later and `max_retries` attempts were exhausted | raise `max_retries`, or set it to `0` for infinite retries |

```python
import aiormq

from protobus import Context, ProtoParseError, ReconnectionError


async def main() -> None:
    context = Context()
    try:
        await context.init(os.environ.get("AMQP_URL", "amqp://localhost"), ["./proto"])
    except ProtoParseError as error:
        raise SystemExit(f"schema failed to load: {error}")
    except aiormq.exceptions.AMQPConnectionError as error:
        raise SystemExit(f"RabbitMQ is not reachable: {error}")
    except ReconnectionError:
        raise SystemExit("gave up reconnecting")
```

> [!NOTE]
> The first connection attempt is not retried: `init()` raises the driver's own exception straight away, and the reconnection machinery only takes over once a connection has been established and then lost. If you want a service to wait for a broker that is still starting, loop on `init()` yourself.

---

## One context per process

One `Context` is one AMQP connection and one schema root. Two contexts in one process is two connections, two callback queues and two copies of every parsed schema, for no gain.

```python
import asyncio

from protobus import ServiceProxy
from context import create_context


async def main() -> None:
    # Every service and proxy in the process shares one context.
    context = await create_context()

    orders = ServiceProxy(context, "Orders.Service")
    users = ServiceProxy(context, "Users.Service")
    await asyncio.gather(orders.init(), users.init())

    await context.close()
```

The reverse — several *services* in one context — is legal but rarely what you want. One asyncio loop is one core, so co-locating services buys no parallelism; it only couples their failure domains and their deploys. Scale with more processes and raise `max_concurrent`.

<details>
<summary><b>What sharing actually saves</b></summary>

<br/>

Per context, at the broker: one connection, one exclusive auto-delete callback queue, and one channel each for the message dispatcher and the event dispatcher. Each `MessageService` adds its own channels on top of that — a request listener, an event listener and a cancel listener.

Per context, in the process: one parsed descriptor pool. Schemas are the expensive half; a second context re-reads and re-parses every file on the paths.

</details>

---

<div align="center">

**[← Configuration](../configuration.md)** · **[Docs index](../../README.md)** · **[MessageService →](./message-service.md)**

</div>
