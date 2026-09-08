# Known Issues

> Current limitations, and the places where this port deliberately differs from the TypeScript one.

| | |
|---|---|
| **Prerequisites** | none |
| **Next** | [Troubleshooting](./troubleshooting.md) · [Architecture](../concepts/architecture.md) |
| **Source** | [`CHANGELOG.md`](../../CHANGELOG.md) · [`tests/integration/test_cross_language.py`](../../tests/integration/test_cross_language.py) |

**On this page** — [Cancellation is cooperative](#cancellation-and-shutdown-are-cooperative) · [No server deadline on a stream](#a-streaming-handler-has-no-server-side-deadline) · [Events are lost by default](#a-failing-event-handler-loses-the-event-unless-retry-is-turned-on) · [The retry hop is unconfirmed](#the-retry-hop-is-not-confirmed) · [A failed settlement may duplicate](#a-failed-settlement-returns-the-message-and-may-duplicate-it) · [No tracing](#no-request-tracing) · [Blocking the loop](#a-blocking-handler-blocks-everything) · [Differences from TypeScript](#differences-from-the-typescript-port) · [Reporting](#reporting-issues)

---

## Cancellation and shutdown are cooperative

**Severity:** Low

**Description:**
Neither the processing timeout nor a stream cancellation can stop a handler
between two of its own awaits — a coroutine cannot be preempted. Both abort the
handler's `signal`, cancel its task where that is safe, and stop the framework
acting on a late result; a handler that never awaits and never checks its
signal runs to completion regardless, and its output is simply discarded.

A graceful shutdown waits for handlers to finish, so a handler that ignores its
signal and runs long will hold shutdown until `SHUTDOWN_DRAIN_TIMEOUT_MS`
elapses, at which point its messages stay unacknowledged and are redelivered.

**Workaround:**
Watch the signal in anything long-running:

```python
async def generateReport(self, request, actor, correlation_id, ctx: MessageHandlerContext):
    for chunk in work_items:
        if ctx.signal.aborted:
            raise RuntimeError("cancelled")
        await process(chunk)
```

Graceful shutdown itself is built in — `RunnableService.start()` installs signal
handlers that stop consuming, drain in-flight work, run your `cleanup()` hook
and then disconnect. See [RunnableService](../reference/api/runnable-service.md).

A stream cancelled by the caller is settled as cancelled work — acknowledged, not retried — whether the producer returned on seeing `signal.aborted` or raised through `signal.throw_if_aborted()`. The cancellation *notice* is best effort and sent once: a lost notice means a producer that runs to completion, and there is no resend API. See [Streaming → Delivery is best effort](../guide/streaming.md#delivery-is-best-effort).

---

## A streaming handler has no server-side deadline

**Severity:** Medium for long-running adapters

**Description:**
`MESSAGE_PROCESSING_TIMEOUT` (and `processing_timeout_ms`) bounds the call
that *creates* a streaming handler's async iterator, not the iteration. A
producer that stalls mid-stream is stopped only by the client's idle timeout
(`STREAM_IDLE_TIMEOUT_MS`), which reaches the server as a cancellation
notice — best effort, and cooperative.

**Workaround:**
Bound the upstream call inside the handler — an HTTP timeout on the model
provider, a cap on tokens — and watch `context.signal`.

---

## A failing event handler loses the event, unless retry is turned on

**Severity:** Medium

**Description:**
By default an event listener registers no retry options, so a handler that
raises takes the no-retry branch: the delivery is rejected without requeue and
the event is gone. It does not climb the retry ladder and never reaches a DLQ.

Rejecting is what keeps the consumer alive: leaving the delivery unacknowledged
would hold the prefetch — **1** unless `max_concurrent` is set — and stall the
listener completely behind the first permanently-failing event. The default is
measured against a real broker in
[`tests/integration/test_events_and_dlq.py`](../../tests/integration/test_events_and_dlq.py)
and set out in full under
[Ack ordering](../concepts/delivery-guarantees.md#ack-ordering).

It stays listed here because the consequence is easy to miss when reading events
as "fire and forget": with the default there is no retry, no dead letter, and no
record afterwards that anything was dropped.

**Workaround:**
Set `event_retry` on the service to give events the same ladder RPC requests
climb — park, redeliver, then `<Service>.Events.DLQ`. It is opt-in because it
declares new topology and because a retry re-runs every handler that matched the
event, not only the one that raised. See
[Turning retry on](../guide/events.md#retry).

Left off, make the handler responsible for its own work: catch and record the
failure somewhere you can replay from, or model the work as an RPC instead.

---

## The retry hop is not confirmed

**Severity:** Low, and inherent to RabbitMQ

**Description:**
A retried message leaves `<Service>.Retry` by TTL expiry and is republished by
the broker's dead-letter mechanism, which does not use publisher confirms. In a
cluster that hop can lose the message silently; nothing in protobus observes
it, and the caller sees only an RPC timeout. Every other transfer is confirmed.
Full account: [Where a message can still be lost](../concepts/delivery-guarantees.md#where-a-message-can-still-be-lost).

**Workaround:**
`RetryOptions(max_retries=0)` for work that must not be lost, and handle the
failure in the handler; treat an RPC timeout as ambiguous either way.

---

## A failed settlement returns the message, and may duplicate it

**Severity:** Low, and inherent to at-least-once

**Description:**
A failed delivery is settled by publishing its retry or DLQ copy and *then*
acknowledging the original. When that publish fails, the original is returned
to the queue after a one-second pause (`basic.reject` with requeue) rather than
left unacknowledged on an open channel, where it would hold the worker's
prefetch credit indefinitely. If the failed publish was ambiguous — a confirm
timeout — the copy may exist as well, so the message can run twice under the
same `message_id`. A missing retry queue or exchange is a hard failure (the
copies are published `mandatory`), never a silent ack into nothing.

A channel the broker closes while the socket stays up — a 404 on a deleted
retry exchange, a 406 on a changed declare — takes its consumer with it; the
listener notices and rebuilds its channel, queue, bindings and retry topology
on the live connection, and the connection's own restoration re-declares that
topology after an outage. Verified against a live broker in
[`tests/integration/test_fresh_broker.py`](../../tests/integration/test_fresh_broker.py).

**Workaround:**
Idempotent handlers keyed on `message_id`, as everywhere else.

---

## No Request Tracing

**Description:**
No built-in support for distributed tracing (e.g., OpenTelemetry, Jaeger).

**Workaround:**
Add tracing manually in your service methods; the `correlation_id` argument is
the natural span attribute, and `message_id` on the handler context is stable
across redeliveries:

```python
async def myMethod(self, request: dict, actor: str, correlation_id: str) -> dict:
    with tracer.start_as_current_span("myMethod", attributes={"correlation_id": correlation_id}) as span:
        try:
            return await self.do_work(request)
        except Exception:
            span.set_status(StatusCode.ERROR)
            raise
```

---

## A blocking handler blocks everything

**Severity:** Medium, and inherent to asyncio

**Description:**
One process is one event loop. A handler that blocks it — a synchronous
database driver, `time.sleep`, a CPU-bound loop — stalls every other in-flight
message on that replica, every event handler, the publish confirms, and the
AMQP heartbeat. Block for longer than two heartbeat intervals (60 s at the
default) and the broker closes the connection under you; the message is then
redelivered to another replica while the first is still working on it.

**Workaround:**
Use async drivers, or hand the blocking part to a thread:
`await asyncio.to_thread(blocking_call, ...)`. For CPU-bound work, a
`ProcessPoolExecutor`, or more replicas with `max_concurrent=1`.

---

## Differences from the TypeScript port

The two ports are wire-compatible and verified against each other in both
directions on every commit. What differs is deliberate, and listed here so
neither port is "fixed" to match the other by accident.

| | protobus-py 2.0 | protobus (TypeScript) 2.4 |
|---|---|---|
| 64-bit integers decode as | `int` | a decimal `string` — JavaScript numbers cannot hold them |
| `timestamp` decodes as | a timezone-aware UTC `datetime` | a `Date` |
| `bytes` decodes as | `bytes` | `Buffer` |
| a processing timeout that exhausts its retries | the caller is answered with `RemoteError`, code `PROCESSING_TIMEOUT` | the caller is left to its own `RpcTimeoutError` |
| a request whose publish was unconfirmed when the socket dropped | republished once on the restored channel, same `message_id`, if the deadline allows | failed with `DisconnectedError` |
| the RPC deadline | bounds readiness, the confirm and the reply; `RpcTimeoutError.published` says how far it got | bounds the reply; a stalled confirm waits out `PUBLISH_CONFIRM_TIMEOUT_MS` |
| a lost channel on a live connection | the listener rebuilds itself; retry/DLQ topology is re-declared on restoration | not recovered |
| a failed retry/DLQ publish | the original is requeued after 1 s; the copies are `mandatory` | the original is left unacknowledged |
| a stream closed before its request was sent | withdrawn: never published | published, then cancelled |
| a validation error's message | names the field and the type only | may include the value |
| closing a stream early | `async with`, or `await stream.aclose()` — `break` alone does not close a Python async iterator | `break` — `for await` calls `return()` |
| a cancelled consumer task | closes the stream and tells the server | n/a |
| event handler arity | `(event)`, `(event, topic)` or `(event, type, topic)`, by inspection | the same three positions, contextually typed |
| generated typing | `TypedDict` + `Protocol` in one module; names carry their package when several packages are exported | `namespace` per package in a `.d.ts` |
| the first connection attempt | not retried — `init()` raises the driver's error | not retried either |
| `StreamClosedError` | exported, never raised | exported, deprecated, never thrown |
| `ServiceCluster` | present — hosts several services in one process | removed in 2.0 |

Wire-level behaviour — envelopes, routing keys, headers, the retry topology,
priority bytes, custom-type encodings — is identical, and is what
[`tests/integration/test_cross_language.py`](../../tests/integration/test_cross_language.py)
checks. CI pins the TypeScript side to the **2.4.0** release commit
(`ba7a9bd`); other 2.x revisions are expected to interoperate but are not
tested. The TypeScript-driven direction covers unary, ordered streaming, an
empty stream and a mid-stream error; the Python-driven direction additionally
covers cancellation (asserted on the TypeScript producer's own counter),
custom types, maps and events.

---

## Reporting Issues

If you encounter issues not listed here:

1. Check existing issues on GitHub
2. Include in your report:
   - protobus-py version (`pip show protobus`)
   - Python version
   - RabbitMQ version
   - Minimal reproduction code
   - Error messages and tracebacks

---

<div align="center">

**[← Queue Migration](./queue-migration.md)** · **[Docs index](../README.md)** · **[Troubleshooting →](./troubleshooting.md)**

</div>
