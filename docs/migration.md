# Migration Guide

> Upgrading from protobus-py 1.x to 2.0 — a release that changes what goes on the wire, what a decoded message looks like, and what happens when a handler fails.

**Read this if** you have a service running on protobus-py 1.x, or a client written against one.

| | |
|---|---|
| **Prerequisites** | A running 1.x service. [Getting Started](./guide/getting-started.md) if you do not have one |
| **Next** | [Delivery Guarantees](./concepts/delivery-guarantees.md) · [Error Handling](./guide/error-handling.md) · [Known Issues](./operations/known-issues.md) |
| **Source** | [`CHANGELOG.md`](../CHANGELOG.md) · [`pyproject.toml`](../pyproject.toml) · [`protobus/__init__.py`](../protobus/__init__.py) |

---

## Version compatibility

| protobus-py | Python | RabbitMQ | AMQP client | Wire-compatible with |
|---|---|---|---|---|
| **2.0** | **≥ 3.10** (CI: 3.10 – 3.13) | 3.8+ | `aiormq` 6.x | TypeScript protobus **2.x** — verified in both directions on every commit |
| 1.5 | ≥ 3.10 | 3.8+ | `aio-pika` 9.x | TypeScript protobus 2.x for unary requests only; events were not compatible |
| 1.2 – 1.4 | ≥ 3.10 | 3.8+ | `aio-pika` | partial |

`protoc` is **no longer needed**, at runtime or at build time. `.proto` files are parsed by the library.

---

## Do I have to do anything?

Yes. Read the "Breaks" column first — *silently* means nothing raises and your code takes a different branch than it did last week.

| Change | Breaks | What to do |
|---|---|---|
| **proto3 zero values decode as `0` / `""` / `False`; unset messages as `None`** | **Silently, at runtime** | Audit every `.get(...)` default, `in` test and `is None` against a decoded request. [Details](#the-dangerous-one-proto3-zero-values) |
| **64-bit integers decode as `int`** (1.5 gave decimal strings) | Silently, if you `int()` them or compare to strings | Drop the conversion |
| **Event handlers take `(event, type, topic)`** | At runtime — a two-argument handler still works, but `topic` is now the *second* argument of a two-argument one | Add the `type` parameter. [Details](#event-handlers) |
| **JSON mode is gone**: a service needs a `.proto` | At `init()`, with `MissingProto` | Write the schema; register custom types for anything protobuf's scalars do not cover |
| **Custom types are chosen by field type, not field name** | Silently, for a field *named* `timestamp` that was not one | Declare the field's type as `timestamp` / `bigint` in the `.proto` |
| Late ack with prefetch 1 is the default | At runtime — failures now retry instead of vanishing | Nothing, unless you relied on at-most-once: `MessageServiceOptions(late_ack=False)` |
| Retry topology changed: `<Service>.Retry` + `<Service>.Retry.Exchange` + `<Service>.DLQ` | At startup, if a 1.5 `<Service>.retry` queue exists with other arguments | Delete the 1.5 `.retry` queue; see [Queue Migration](./operations/queue-migration.md) |
| Requests and events are persistent | Nothing | Nothing |
| RPC requests are `mandatory` | At runtime — `UnroutableError` where there was a timeout | Handle it; it is the more useful error |
| `ServiceProxy` raises `RemoteError` for a remote error | At runtime, for `except Exception as e: e.args[0]` code | Catch `RemoteError`; read `.message` and `.code` |
| Delivery errors keep their type | At runtime, for `except PublishMessageError` | It is now an alias of `PublishError`, the base of every delivery error — the handler still catches them |
| `Context.init(url, proto_dirs=[...])` → `Context.init(url, [...])` | Nothing — the keyword is still accepted | Prefer the positional form |
| `MessageFactory.init()` is synchronous | `await factory.init()` raises `TypeError` | Drop the `await` (Context does this for you) |
| Debug logging off by default | At runtime — log volume drops | `LOG_LEVEL=debug` to restore |
| `RPC_CALL_TIMEOUT_MS` defaults to 600 000 (was 30 000 in 1.5) | Timing | Set it, or pass `timeout_ms` per call |
| Stream cancellation declares `proto.bus.cancel` | At runtime, if broker permissions forbid it | Grant `configure` on it, or accept the logged warning |
| A `break` out of a streaming `async for` no longer cleans up by itself | Silently — the server keeps producing until the idle timeout | Use `async with proxy.stream(...) as chunks:` or `await stream.aclose()`. [Details](#streaming) |
| `aio-pika` is no longer a dependency | At import, if *your* code imported it through protobus | Depend on it yourself, or use `aiormq` |

---

## The dangerous one: proto3 zero values

> [!CAUTION]
> This changes which branch your code takes **without raising anywhere**.

proto3 omits any scalar equal to its default from the wire. 1.x decoded through protobuf's JSON mapping *without* materialising defaults, so a legitimate `0`, `""` or `False` arrived as a missing key. 2.0 decodes to what the TypeScript port produces: every scalar present, an unset message field `None`, repeated fields `[]`, maps `{}`.

Given `message R { int32 count = 1; string cursor = 2; bool dry = 3; Inner inner = 4; repeated int32 ids = 5; int64 big = 6; }` and nothing set:

| | 1.x | 2.0 |
|---|---|---|
| decoded dict | `{'ids': []}` | `{'count': 0, 'cursor': '', 'dry': False, 'inner': None, 'ids': [], 'big': 0}` |
| `big` set to 7 | `'7'` (a string) | `7` |

| Your code | 1.x | 2.0 | |
|---|---|---|---|
| `not request.get('count')` | `True` | `True` | safe |
| `request.get('count') or 10` | `10` | `10` | safe |
| `request.get('count', 10)` | `10` | **`0`** | **changed** |
| `'count' in request` | `False` | **`True`** | **changed** |
| `request['count']` | `KeyError` | **`0`** | **changed (for the better)** |
| `request.get('inner') is None` | `True` | `True` | safe |
| `'inner' in request` | `False` | **`True`** | **changed** |
| `int(request['big'])` | `7` | `7` | safe, but redundant |

The rule of thumb: **`or` and `not` are safe because `0`, `""` and `False` are falsy either way. `.get(key, default)` and `in` are not, because they distinguish absent from zero — and that distinction moved.**

If you genuinely need to distinguish "unset" from "zero", declare the field `optional` in proto3; it is then present in the dict only when set. The 1.x behaviour was an accident of the JSON mapping, not field presence.

---

## Event handlers

1.x called an event handler as `handler(data, topic)`. 2.0 matches the TypeScript port: `handler(event, type, topic)`. The framework looks at the handler's arity:

```python
async def three(event, event_type, topic): ...   # the 2.0 shape
async def two(event, topic): ...                 # still called, with (event, topic)
async def one(event): ...                        # still called, with (event,)
```

A two-argument handler keeps working. What changes is that events are now typed on the wire, so the `.proto` must declare the event message and `publish_event`'s first argument is its fully-qualified name (`"Combat.PlayerShot"`, not `"PlayerShot"`).

---

## The retry ladder

1.5 published a failed message to `<Service>.retry` (lowercase) through the default exchange, which dropped the routing key; the redelivery arrived with a wildcard key the TypeScript port rejects. 2.0 declares what the TypeScript port declares:

| Object | Purpose |
|---|---|
| `<Service>.Retry` | TTL queue, `x-message-ttl = retry_delay_ms`, dead-letters to `proto.bus` |
| `<Service>.Retry.Exchange` | topic exchange the retry queue is bound to with `#`; publishing here keeps the original routing key |
| `<Service>.DLQ` | where the message lands after `max_retries` hops |

A 1.5 `<Service>.retry` queue is simply unused. Delete it once nothing is parked on it. If you had changed `retry_delay_ms` and a `<Service>.Retry` queue already exists with the old TTL, `init()` raises `RetryQueueMismatchError` — see [Queue Migration](./operations/queue-migration.md).

---

## Streaming

A Python async iterator is not closed by `break`. In 1.x that was invisible because nothing told the server anyway; in 2.0 closing the stream is what cancels the producer, so make the close explicit:

```python
async with proxy.generate({"prompt": p}) as stream:     # closes on exit, break included
    async for token in stream:
        ...
        if enough:
            break
```

or pass an `AbortSignal` — `StreamOptions(signal=controller.signal)` — and `controller.abort()` from anywhere. A stream that is neither closed nor aborted is released by the idle timeout (`STREAM_IDLE_TIMEOUT_MS`), which also sends the cancel.

---

## Upgrade checklist

1. `pip install -U protobus` (2.0 pulls `aiormq`; drop `aio-pika` from your own requirements if only protobus needed it).
2. Give every service a `.proto`; move custom-type fields from name-based to type-based declarations.
3. Grep handlers for `.get(` with a default, `in request`, and `int(`/`str(` on 64-bit fields.
4. Change event handlers to `(event, type, topic)` and event names to fully-qualified message names.
5. Replace `except Exception` around proxy calls with `except RemoteError` where you read the message, and `except PublishError` where you retry.
6. Wrap streaming loops in `async with`.
7. Delete stale `<Service>.retry` queues; if `retry_delay_ms` changed, migrate `<Service>.Retry`.
8. Run your suite against a broker: `python -m pytest tests/integration` in this repo shows the shape of such a test.

## How to tell you are done

- `LOG_LEVEL=debug` shows `started consuming from <Service>` and `<Service>.Events` on startup and no `MissingProto`.
- A deliberately failing handler produces one message on `<Service>.DLQ` with `x-retry-count = max_retries`, and the caller receives a `RemoteError` rather than an `RpcTimeoutError`.
- A TypeScript peer, if you have one, exchanges events with the Python service in both directions.
