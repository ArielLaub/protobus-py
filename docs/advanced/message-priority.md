# Message Priority

A protobus service gets **one** queue, bound to `REQUEST.<ServiceName>.*`, and RabbitMQ
delivers it FIFO. That is usually what you want. It stops being what you want the moment a
service publishes its own bulk work onto its own queue: a control RPC that fans out
thousands of per-item messages puts every *subsequent* control RPC behind the entire
fan-out. The second call is accepted, sits in line behind ~5,000 messages, and fails at its
deadline — with every replica healthy and consuming the whole time.

Message priority fixes that without a second service and without a second queue: control
messages are published at a higher priority and jump the bulk traffic already queued.

> **Read [What it does NOT do](#what-it-does-not-do) before relying on this.** Priority
> reorders the queue, not the consumers' prefetch buffers. It shrinks the window by orders
> of magnitude; it does not eliminate it.

---

## ⚠️ Enabling priority on an existing queue requires an operator migration

**RabbitMQ cannot add `x-max-priority` to a queue that already exists.** Re-declaring a live
queue with a new argument set is answered with `406 PRECONDITION_FAILED`, and a 406 closes
the channel. Protobus shares one connection across every listener in a process, so this is
not a local error — it is a service-wide outage on deploy.

Verified against RabbitMQ 3:

```
PRECONDITION_FAILED - inequivalent arg 'x-max-priority' for queue 'svc.Example'
in vhost '/': received the value '2' of type 'byte' but current is none
```

So enabling priority on a service that is already deployed is a **one-time, operator-run**
sequence:

1. Stop the service's consumers (scale to 0).
2. Let the queue drain, or accept that you are discarding what is in it.
3. `rabbitmqctl delete_queue <ServiceName>` (or delete it in the management UI).
4. Deploy the version that passes `max_priority`. It declares the queue fresh, with the
   argument.

It is a **one-queue** migration. The `.retry` and `.DLQ` queues are deliberately left
untouched — see [Retries](#retries-and-dead-lettering).

Re-declaring with the **same** `x-max-priority` is idempotent, so once migrated, ordinary
restarts and redeploys are safe. Only a *change* to the argument set 406s.

---

## Enabling it

Opt in per service, on the service options:

```python
from protobus import Config, MessageServiceOptions, RunnableService

class RecommendationsService(RunnableService):
    @property
    def service_name(self) -> str:
        return "Onit.Recommendations"

options = MessageServiceOptions(
    max_concurrent=10,
    max_priority=Config.RECOMMENDED_MAX_PRIORITY,   # 2
)
await RecommendationsService.start(ctx, RecommendationsService, options)
```

`max_priority` defaults to `None`, which declares the queue with **no** `x-max-priority`
argument at all — byte-identical to a service built before this feature existed. That
default is load-bearing and will not change; see [Compatibility](#compatibility).

## Publishing at a priority

Keyword-only, on any proxy method:

```python
proxy = ServiceProxy(ctx, "Onit.Recommendations")
await proxy.init()

# control traffic — jumps the queue
await proxy.publish({"catalog": "v4"}, actor, priority=Config.PRIORITY_CONTROL)

# bulk fan-out — the default, unchanged
await proxy.recalculateUser({"userId": uid}, actor, rpc=False)
```

## The three levels

```python
Config.PRIORITY_NORMAL           # 0 — bulk work. Also what RabbitMQ assumes
                                 #     for a message published with no priority.
Config.PRIORITY_HIGH             # 1 — reserved middle rung.
Config.PRIORITY_CONTROL          # 2 — control-plane RPCs.
Config.RECOMMENDED_MAX_PRIORITY  # 2 — the value to pass as max_priority.
```

**Keep the range tiny.** RabbitMQ implements a priority queue as one internal sub-queue per
level, so each level costs memory and throughput whether or not you use it. The broker's own
guidance is to stay in single digits; `x-max-priority` above 10 is legal but wasteful, and
above 255 is a 406. Two rungs cover the motivating case; the third is there so adding one
later is not a breaking migration.

Publishing a priority **above** a queue's `x-max-priority` is not an error. RabbitMQ clamps
it to the ceiling *for ordering* — a priority-5 message on a `x-max-priority: 2` queue sorts
exactly as if it were 2, and stays FIFO with respect to genuine priority-2 messages — while
the `priority` property itself is stored unchanged. Verified; there is nothing to be gained
by publishing above the ceiling.

---

## What it does NOT do

**Priority reorders only the messages still sitting in the queue.** A message that has
already been handed to a consumer is not reordered, and consumers hold more than one:
`max_concurrent` sets a prefetch, so each replica keeps that many messages in flight or
buffered locally.

With prefetch `N` across `R` replicas, up to **N × R** bulk messages can still be ahead of a
high-priority message that arrives after them. Concretely, at `max_concurrent=10` across 3
replicas, a control message can still wait behind ~30 bulk messages — instead of 5,000.

That is a two-orders-of-magnitude improvement and it is usually the difference between
"breaches its deadline" and "fine". It is **not** a guarantee of immediate handling. If you
need a hard bound, lower the prefetch — the two trade off directly against each other.

Also worth knowing:

- Priority does nothing on a queue declared without `x-max-priority`. It is silently
  ignored, which is exactly what makes it safe (see below).
- Priority is per-queue ordering only. It has no effect on which replica gets a message, or
  on how long a handler takes once it starts.

## Retries and dead-lettering

The `.retry` and `.DLQ` queues do **not** get an `x-max-priority` of their own, on purpose.

A message keeps its `priority` property across a dead-letter hop — verified against a real
broker. So a retried control message still carries priority 2 when the retry queue's TTL
expires it back onto the main queue, and it re-sorts correctly there. Ordering *within* the
retry queue is by TTL expiry, not priority, so a ceiling there would buy nothing — and it
would turn a one-queue operator migration into a three-queue one.

## Compatibility

Everything here is additive and opt-in, in both directions:

| Scenario | Behaviour |
|---|---|
| Old service, old client | Unchanged. |
| **New** service that does not pass `max_priority` | Declares its queue with the identical argument set as before. No migration, no 406. |
| **New** client publishing a priority to an **old** (non-priority) queue | Accepted by the broker and ignored for ordering. No error, channel stays open. This is what lets clients be deployed before services. |
| Old client calling a **new** priority-enabled service | Works. Its messages carry no explicit priority, which RabbitMQ treats as 0 — the same as `PRIORITY_NORMAL`. |
| TypeScript publisher ↔ Python consumer (and vice versa) | Same wire behaviour. See below. |

### A note on the Python/TypeScript wire difference

The TypeScript port omits the AMQP `priority` property entirely when no priority is given.
aio-pika does not offer that: it normalizes an unset priority to `0`
(`optional(priority, int, 0)`), so **protobus-py has always put `priority: 0` on every
message**, long before this feature existed.

This is a difference in the bytes, not in the behaviour. RabbitMQ treats an absent priority
and an explicit `0` as the same lowest priority — pinned by a real-broker test that publishes
one of each to a priority queue and asserts they keep their relative order while a
priority-2 message jumps both. Nothing needs to be done about it; it is documented here so
nobody re-derives it from a packet capture and thinks the ports disagree.

## Validation

Both values are validated at the protobus seam, raising `InvalidPriorityError` (a
`ValueError`):

- `max_priority` — integer, 1..255. Checked at listener construction, **before** anything is
  sent, because the failure it prevents is a channel-killing 406 on the shared connection.
- `priority` — integer, 0..255.

The integer check is not pedantry. aio-pika applies `int()` to whatever it is handed, so
`priority=1.5` would otherwise be stored as `1` with no error anywhere; and an out-of-range
value would surface much later as a raw `struct.error` from the encoder rather than as
something a caller can act on. The TypeScript port validates at the same seam, for the same
reasons, so both ports reject the same inputs.
