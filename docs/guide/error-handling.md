# Error Handling

> Which failures protobus retries, which it answers, and how each one reaches the caller.

**Read this if** you are writing a handler and need to decide what to raise — or you have a message stuck in a retry loop.

| | |
|---|---|
| **Prerequisites** | [Getting Started](./getting-started.md) |
| **Next** | [Delivery Guarantees](../concepts/delivery-guarantees.md) — the mechanism · [Errors reference](../reference/errors.md) — every class |
| **Source** | [`protobus/errors.py`](../../protobus/errors.py) · [`protobus/message_service.py`](../../protobus/message_service.py) · [`protobus/connection.py`](../../protobus/connection.py) |

**On this page** — [The one decision](#the-one-decision) · [Terminal failures](#terminal-failures-handlederror) · [Retriable failures](#retriable-failures-anything-else) · [What the caller sees](#what-the-caller-sees) · [Tuning the ladder](#tuning-the-retry-ladder) · [Events are different](#events-are-different) · [Anti-patterns](#anti-patterns)

---

## The one decision

Every exception from a handler answers a single question: **would running this again produce a different result?**

| Answer | Raise | What protobus does |
|---|---|---|
| No — the same input fails the same way | `HandledError` | replies to the caller immediately, rejects the message without requeue. **No retry.** |
| Maybe — a dependency was briefly unavailable | any other exception | parks the message on `<Service>.Retry` and redelivers it, up to `max_retries` times, then dead-letters it |

Getting this wrong is expensive in both directions. A validation failure raised as
a plain `Exception` retries four times over fifteen seconds and dead-letters a
message that was never going to succeed; a genuine outage raised as a
`HandledError` fails a request that a second attempt would have served.

---

## Terminal failures: `HandledError`

```python
from protobus import HandledError, RunnableService


class OrderService(RunnableService):
    service_name = "Orders.Service"

    async def createOrder(self, request: dict, actor: str, correlation_id: str) -> dict:
        if not request.get("orderId"):
            raise HandledError("orderId is required", "VALIDATION_ERROR")
        return {"ok": True}
```

Use it for anything the caller could fix and a retry could not: invalid input, a
resource that does not exist, a permission denial, a business rule violation.

Subclass it when you want the code in one place:

```python
from protobus import HandledError


class ValidationError(HandledError):
    def __init__(self, message: str):
        super().__init__(message, "VALIDATION_ERROR")


class NotFoundError(HandledError):
    def __init__(self, resource: str, id: str):
        super().__init__(f"{resource} {id} not found", "NOT_FOUND")
```

> [!NOTE]
> `is_handled_error(err)` is duck-typed — it accepts any exception with
> `is_handled = True` ([`protobus/errors.py`](../../protobus/errors.py)). An error
> crossing a module boundary, or one from a differently-installed copy of
> protobus, still classifies correctly.

---

## Retriable failures: anything else

```python
import httpx

from protobus import RunnableService


class ReportService(RunnableService):
    service_name = "Reports.Service"

    async def build(self, request: dict, actor: str, correlation_id: str) -> dict:
        async with httpx.AsyncClient() as client:
            upstream = await client.get("https://example.invalid/" + request["id"])
        if upstream.status_code >= 500:
            # Plain exception: the upstream may well be back in five seconds.
            raise RuntimeError(f"upstream returned {upstream.status_code}")
        return {"url": upstream.text}
```

The failure then climbs the retry ladder: `<Service>.Retry` holds the message for
`retry_delay_ms`, its TTL expires, the dead-letter exchange returns it to the
service's own queue, and the handler runs again. After `max_retries` failures the
message goes to `<Service>.DLQ` carrying headers that say why.

> [!IMPORTANT]
> **The caller stays parked for the whole ladder.** No reply is published while a
> message is being retried, so with the defaults — `max_retries=3`,
> `retry_delay_ms=5000` — a permanently failing call blocks its caller for roughly
> **15 seconds** before it is told anything. The full mechanism, the six `x-*`
> headers, and how this interacts with `RPC_CALL_TIMEOUT_MS` are in
> [Delivery Guarantees](../concepts/delivery-guarantees.md).

A handler that exceeds `MESSAGE_PROCESSING_TIMEOUT` (or the service's
`processing_timeout_ms`) is cancelled and treated the same way — it climbs the
ladder as a `TimeoutError` with code `PROCESSING_TIMEOUT`, and the caller is
told so once the ladder is exhausted.

---

## What the caller sees

A `ServiceProxy` call raises a **`RemoteError`** carrying `message`, `code` and
`method` — not an instance of your class. The class does not survive the wire;
the `code` you set on `HandledError` does.

```python
from protobus import RemoteError, ServiceProxy


async def create(proxy: ServiceProxy, order_id: str | None) -> dict | None:
    try:
        return await proxy.createOrder({"orderId": order_id})
    except RemoteError as error:
        # Switch on the code you set, not on the error's text.
        if error.code in ("VALIDATION_ERROR", "NOT_FOUND"):
            return None
        raise
```

> [!WARNING]
> **Do not match on `error.message` text.** The message is a plain string, and
> matching substrings breaks the first time someone rewords a message. `code`
> exists precisely so you do not have to.

What reaches the caller for a *non*-`HandledError` depends on
`PROTOBUS_EXPOSE_INTERNAL_ERRORS`, which defaults to `true` — the unhandled
error's own message is sent. Set it to `false` and the caller gets an
`InternalServiceError` message carrying a correlation id instead, with code
`INTERNAL_ERROR`. See [Security](../operations/security.md) and
[Configuration](../reference/configuration.md).

---

## Tuning the retry ladder

```python
from protobus import Context, MessageServiceOptions, RetryOptions, RunnableService


class OrdersService(RunnableService):
    service_name = "Orders.Service"

    def __init__(self, context: Context):
        super().__init__(context, MessageServiceOptions(
            max_concurrent=4,
            retry=RetryOptions(
                max_retries=5,        # default 3
                retry_delay_ms=2000,  # default 5000
            ),
        ))
```

`RunnableService.start()` and `launch()` accept the same options as keyword
arguments — `await OrdersService.start(context, max_concurrent=4, retry=RetryOptions(max_retries=5))`
— when you would rather not override `__init__`.

| Option | Default | Effect |
|---|---:|---|
| `max_retries` | `3` | attempts after the first failure. **`0` disables retry entirely** — no `.Retry` or `.DLQ` queue is declared, and a failure is answered and rejected |
| `retry_delay_ms` | `5000` | the TTL on `<Service>.Retry`, so the delay is fixed, not exponential |
| `message_ttl_ms` | `None` | a total lifetime for the message; past it the broker discards it regardless of retries left |

> [!CAUTION]
> `retry_delay_ms` becomes the retry queue's `x-message-ttl`, and RabbitMQ refuses
> to redeclare a queue with different arguments. Changing it against an existing
> deployment raises `RetryQueueMismatchError`. See
> [Queue Migration](../operations/queue-migration.md).

---

## Events are different

> [!CAUTION]
> **By default a failing event handler is not retried, and the event is not
> dead-lettered — it is discarded.** `EventListener` supplies no retry options
> unless the service opts in ([`protobus/event_listener.py`](../../protobus/event_listener.py)),
> so an exception takes the reject-without-requeue branch in
> [`protobus/connection.py`](../../protobus/connection.py) and leaves only a
> logged error behind. The `<Service>.Events` queue being durable does not
> change this.

That makes an event handler's error policy your responsibility:

```python
from protobus import RunnableService


class OrderProjection(RunnableService):
    service_name = "Orders.Projection"

    async def init(self) -> None:
        await super().init()
        await self.subscribe_event("Orders.OrderCreated", self.on_order_created)

    async def on_order_created(self, event: dict) -> None:
        try:
            await self.project(event)
        except Exception as error:
            # Nothing downstream will retry this. Persist enough to
            # reprocess deliberately, and do not re-raise expecting a requeue.
            await self.record_failure(event, error)

    async def project(self, event: dict) -> None: ...
    async def record_failure(self, event: dict, error: Exception) -> None: ...
```

If an event genuinely needs at-least-once processing with retries, either opt in
to the event retry ladder — `MessageServiceOptions(event_retry=EventRetryOptions(...))`,
see [Events → Turning retry on](./events.md#retry) — or model it as an RPC to a
service that owns the work, and let the request queue's ladder do its job.

---

## Anti-patterns

**Republishing the event to retry it yourself.** There is no retry count on
the wire, the republished event is a new message that every other subscriber
also receives again, and it competes with nothing. If you need delayed
reprocessing, opt in to `event_retry`, or write the failure down and reprocess
from your own store.

**Wrapping everything in `try/except` and returning a success shape.** A
handler that swallows a failure and returns `{"ok": False}` is invisible to the
retry ladder, to the DLQ, and to every metric derived from them. Raise.

**Catching an error only to re-raise a `HandledError`.** That converts a
transient failure into a permanent one. Only do it when you have established the
failure is not transient.

**A hand-rolled circuit breaker around a proxy call.** Reasonable in general, but
it belongs in your application code and is not protobus-specific — see
[Patterns](./patterns.md#resilience-patterns).

---

<div align="center">

**[← Events](./events.md)** · **[Docs index](../README.md)** · **[Delivery Guarantees →](../concepts/delivery-guarantees.md)**

</div>
