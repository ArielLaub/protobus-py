# Message Priority

A service has **one** request queue. Protobus binds it to `REQUEST.<service_name>.*`,
so every method of a service shares that queue and RabbitMQ delivers them FIFO.

That is usually what you want, and occasionally ruinous. The case this feature
exists for: a service whose "start the job" RPC fans out one message per user
onto its own queue. The fan-out is thousands of messages long, and the *next*
control message — a second start, a cancel, a status request — lands behind all
of them and breaches its deadline while every replica is healthy and busy.

The shape it was diagnosed in: three control calls issued during one 5,232-
message drain, all three accepted by the broker, all three timed out at their
deadline, both replicas connected and consuming the whole time. Nothing was
broken. The queue was simply one lane, and the lane was full.

Message priority fixes that without a second service and without a second queue:
the control message is published at a higher priority and overtakes the bulk
traffic still sitting in the queue.

> **Priority is opt-in and OFF by default.** A service that does not ask for it
> declares its queue exactly as every previous version of protobus did. See
> [Backward compatibility](#backward-compatibility) — the guarantee is precise,
> and the one thing it does *not* cover is enabling priority on a queue that
> already exists.

## Using it

Two halves. The consumer declares its queue as a priority queue; the publisher
marks individual messages.

### 1. Declare the queue with `max_priority`

```python
from protobus import Config, Context, MessageServiceOptions, RunnableService


class RecommendationsService(RunnableService):
    service_name = "Recommendations.Service"

    def __init__(self, context: Context):
        super().__init__(context, MessageServiceOptions(max_priority=Config.RECOMMENDED_MAX_PRIORITY))  # 2
```

`max_priority` becomes the queue's `x-max-priority` argument. It must be an
integer from 1 to 255 (a `bool` is refused); anything else raises
`InvalidPriorityError` at construction, before any broker I/O.

**`max_priority` requires `late_ack`, which is the default.** Passing
`late_ack=False` alongside it raises. Priority reorders what is still in the
*queue*, and RabbitMQ applies no QoS prefetch to an auto-ack consumer — so an
early-ack consumer is handed the entire backlog and there is nothing left to
reorder. This is refused rather than warned about because the failure is
invisible: the queue is correctly declared, the operator has already done the
one-time migration to enable it, and the feature simply does nothing.

**Keep the number small.** RabbitMQ maintains internal structures per priority
level, so a large range costs memory and throughput and buys nothing.
`Config.RECOMMENDED_MAX_PRIORITY` is **2**, giving three levels, which is one
more than any known use needs:

| Constant | Value | For |
|---|---:|---|
| `Config.PRIORITY_NORMAL` | 0 | Bulk work. Also what an unset priority means. |
| `Config.PRIORITY_HIGH` | 1 | Spare rung. |
| `Config.PRIORITY_CONTROL` | 2 | Control messages that must not queue behind bulk. |

#### Why three levels, and why named

The set is deliberately small and deliberately fixed. Four reasons, in the
order they carry weight:

- **A priority level is not free.** RabbitMQ builds internal data structures per
  level, so `x-max-priority: 10` costs memory and throughput for seven levels
  nobody publishes to. [RabbitMQ's own
  documentation](https://www.rabbitmq.com/docs/priority) recommends keeping the
  number small for exactly this reason.
- **Three covers the distinction that actually exists.** Bulk work, elevated,
  control. `PRIORITY_HIGH` is already a spare rung, kept because leaving a gap
  is cheaper than a migration later.
- **A named constant documents itself at the call site.** `priority=2` in a
  publish tells a reader nothing; `priority=Config.PRIORITY_CONTROL` tells them
  why the call is there. Values are integers on the wire, but callers should not
  be writing integers.
- **A fixed set keeps the two ports identical.** Python and
  [TypeScript](https://github.com/ArielLaub/protobus) services talk to each
  other over the same queues, so the levels have to mean the same thing in both;
  a per-project priority vocabulary is a per-project disagreement waiting to
  happen.

Underneath all four: priority is a coarse instrument. It only reorders what is
still **in the queue** (see [What priority does not
do](#what-priority-does-not-do)), so a wide range invites callers to encode a
fine-grained scheduling policy into a mechanism that cannot honour it. Two
neighbouring levels rarely produce two distinguishable outcomes.

### 2. Publish with a `priority`

Unary and fire-and-forget proxy methods take a `CallOptions` as their last
argument. (Streaming methods take `StreamOptions` in that position instead and
cannot carry a priority — see [Scope](#scope).)

```python
from protobus import CallOptions, Config, ServiceProxy

recs = ServiceProxy(context, "Recommendations.Service")
await recs.init()

# Control message — overtakes the backlog.
await recs.processSingleRecommendation(
    {"rule_key": rule_key}, actor, True, None, CallOptions(priority=Config.PRIORITY_CONTROL),
)

# The fan-out this control message produces — ordinary bulk traffic.
await recs.processUserSingleRecommendation(
    {"user_id": user_id, "rule_key": rule_key}, actor, False, None, CallOptions(priority=Config.PRIORITY_NORMAL),
)
```

The signature is `(request, actor=None, rpc=True, timeout_ms=None, options=None)`.
`options` is appended last, so every existing call is unchanged; the keyword
`priority=` accepted by 1.x still works and is folded into `options`.

`priority` must be an integer from 0 to 255 or `InvalidPriorityError` is raised.
Protobus validates it rather than passing it straight to the driver because the
priority is encoded in a single byte on the wire and a non-integer would be
**silently truncated** somewhere below: `1.5` would reach the broker as `1`,
with no error anywhere.

A priority above the queue's `x-max-priority` is not an error and not useful:
the broker clamps it **for ordering** while preserving the property as sent. On
an `x-max-priority: 2` queue, a message published at 5 sorts as a 2 — so it goes
behind an earlier 2 rather than ahead of it — and still reads back as 5.

### Scope

Priority applies to the **RPC request path**: unary calls and fire-and-forget
publishes. It is deliberately not plumbed through events or streaming calls;
neither has a demonstrated need, and every surface added here has to stay
identical to the [TypeScript port](https://github.com/ArielLaub/protobus) forever.

## What priority does not do

**Priority reorders messages that are still in the queue. It cannot reach a
message the broker has already handed to a consumer.**

Each consumer holds up to `max_concurrent` unacknowledged messages (its
prefetch). Those are already out of the queue and will be worked through
regardless of what arrives later. With prefetch `N` across `R` replicas, up to
`N × R` bulk messages can still sit ahead of a control message.

The integration test in [`tests/integration/test_priority.py`](../../tests/integration/test_priority.py)
is written to show this rather than hide it: with prefetch 1, a control message
published *after* 30 bulk messages is handled **second**, not first. The one
ahead of it is the one already in the consumer's hands. That test counts
messages; the TypeScript repository's `message_priority_latency.test.ts` times
them, which turns out to matter — see
[The count is not the wait](#the-count-is-not-the-wait).

So the honest claim is a change of scale, not a guarantee:

| | Bulk messages ahead of a control message |
|---|---|
| Without priority | the whole backlog — thousands |
| With priority | at most `max_concurrent × replicas` — typically single digits |

If you need a hard bound on that *count* rather than a large improvement,
priority is not the mechanism. But the count is rarely what you actually care
about — see [The count is not the wait](#the-count-is-not-the-wait) below.

When the consumer is saturated the bound above is not merely an upper limit —
it is an equality. Measured, one replica, a 50-message backlog, only the
prefetch varying, with every prefetched delivery held in its handler:

| `max_concurrent` | Control message handled at |
|---:|---|
| 1 | index **1** |
| 5 | index **5** |
| 20 | index **20** |

The control message emerges at *exactly* the prefetch. Measured independently
in both ports, with the same result.

**The equality holds while the consumer is saturated** — that is, while all
`max_concurrent` slots are genuinely occupied by in-flight handlers. That is the
case this feature exists for: a slow handler with work queueing up behind it. If
handlers instead finish faster than messages arrive, slots keep freeing and the
consumer simply drains the backlog; the control message can then be handled much
later than `max_concurrent` (measured: index 49 of 51 at a prefetch of 5) because
the queue it would have jumped was already consumed while it was in flight. That
case is not a problem — a backlog that drains in milliseconds is not a backlog —
but it does mean a benchmark with a fast handler measures something other than
this limit.

### The count is not the wait

The obvious reading of that table is that a large `max_concurrent` erodes the
benefit: twenty messages ahead of you must mean twenty task durations of
waiting. That reading is wrong, and it is worth being exact about why, because
it leads to precisely the wrong tuning decision.

**Those prefetched messages are being worked concurrently.** They are not
queued in front of the control message, they are running *beside each other*.
The control message waits for **one slot to free** — about one task duration —
whether that slot is one of three or one of thirty.

And the more slots there are, the more often one of them frees, so extra
parallelism can only shorten that wait — never lengthen it. Measured against a
live broker (TypeScript port, one replica, a 30-message backlog of one-second
handlers — the [worked example](#a-worked-example) below):

| `max_concurrent` | Control message at | Handled after | Whole batch |
|---:|---:|---:|---:|
| 3 | index 3 | **965 ms** | 9.1 s |
| 10 | index 10 | **967 ms** | 2.0 s |
| 3, no priority | index 30 | **9,993 ms** | 10.0 s |

The index tripled with the prefetch. The wait moved by two milliseconds, which
is noise — and two milliseconds is the *floor*, not a coincidence: in a flood
every slot starts at once and so frees at once, one task duration later. In
steady state, with completions staggered across the slots, a slot frees more
often than that and the control message is picked up sooner still.

So `max_concurrent` is the width of the window priority cannot see into measured
*in messages* — and that width costs no time. Tune it for throughput; it is not
a priority knob, and turning it down to "tighten" the bound buys a smaller
number and a slower service.

**Where priority genuinely does nothing is the other end**: a prefetch large
enough that the entire backlog has already been dispatched. Nothing is left in
the queue, so there is nothing to reorder and the control message is handled in
publish order like everything else. Measured directly: `max_concurrent=100`
against a 50-message backlog put the control message at index 50, priority
fully inert. The condition is not "a large prefetch", it is
"a prefetch that is large relative to the backlog", and a backlog smaller than
the prefetch is by definition not the problem this feature solves.

Two more limits worth knowing:

- **Priority is per queue, not global.** It orders one service's own queue and
  says nothing about how the broker schedules between services.
- **A starved low-priority message is never delivered.** If high-priority
  traffic never stops, the bulk backlog never drains. This is fine for control
  traffic, which is rare by definition, and a hazard if you promote a whole
  traffic class.

## A worked example

The numbers above come from the TypeScript repository's
`message_priority_latency.test.ts`; the Python suite's
[`tests/integration/test_priority.py`](../../tests/integration/test_priority.py)
asserts the same ordering by position. Run it with a broker up:

```bash
docker compose up -d --wait
python -m pytest tests/integration/test_priority.py
```

The service is the shape the feature exists for: one expensive method that
fills the queue and one cheap one that has to get through anyway, sharing the
single queue a protobus service has.

```python
class WorkService(MessageService):
    service_name = "Work.Service"

    def __init__(self, context: Context, prefetch: int, max_priority: int | None = None):
        super().__init__(context, MessageServiceOptions(max_concurrent=prefetch, max_priority=max_priority))

    async def slow(self, request: dict, actor: str, correlation_id: str) -> dict:
        """The bulk work: a second of it, per message."""
        await asyncio.sleep(1)
        return {"tag": request["tag"]}

    async def fast(self, request: dict, actor: str, correlation_id: str) -> dict:
        """The control call: cheap, and latency-sensitive."""
        return {"tag": request["tag"]}
```

Flood the slow method, wait until every prefetch slot is genuinely busy — the
test asserts this rather than assuming it, because an unsaturated consumer just
drains the backlog and the run measures nothing — then send the control call:

```python
for i in range(30):
    await proxy.slow({"tag": f"bulk-{i}"}, None, False, None, CallOptions(priority=Config.PRIORITY_NORMAL))
# ... 30 seconds of work, three slots, all three busy ...

await proxy.fast({"tag": "CONTROL"}, None, False, None, CallOptions(priority=Config.PRIORITY_CONTROL))
```

Measured against RabbitMQ 3, one replica (TypeScript port; the Python handler path is the same shape):

```
prefetch 3, priority:     control handled after   965ms, at index  3 of 31; whole batch  9055ms
prefetch 10, priority:    control handled after   967ms, at index 10 of 31; whole batch  2031ms
prefetch 3, no priority:  control handled after  9993ms, at index 30 of 31; whole batch 10040ms
```

Which is the whole feature in three lines: **about a second**, because that is
how long it takes for one of the parallel slots to free, against ten seconds of
waiting for the batch to finish. Tripling the prefetch tripled the index and
left the wait alone.

The third line is also the mutation check — the identical scenario with the
priority taken off the call — and it is a real test, not a snippet: dropping
`CallOptions(priority=Config.PRIORITY_CONTROL)` from the first case takes it
from 965 ms to 9,999 ms and fails its assertion, and so does leaving the
priority on the call while dropping `max_priority` from the queue. Both halves
are load-bearing — `test_priority.py` checks both.

## Backward compatibility

The four guarantees below are each pinned by a test.

**1. Opt-in only.** With `max_priority` unset, `x-max-priority` is absent from
the queue arguments entirely — not present-and-`None`. A service that does
not ask for priority declares `{}` (or `{'x-message-ttl': …}`), byte-identical
to the version before this feature existed, so its existing queue redeclares
cleanly on upgrade.

**2. A `priority` sent to a non-priority queue is ignored, not rejected.**
Verified against RabbitMQ 3: the message is delivered in FIFO order, the
`priority` property is preserved on it, and no channel error occurs. This is
what lets an upgraded publisher run against a consumer that has not been
upgraded yet.

**3. Both directions interoperate.** New publisher → old consumer is guarantee 2.
Old publisher → new consumer works because an unset priority is treated by
RabbitMQ as 0, which is exactly `PRIORITY_NORMAL`.

**4. No signature changed.** `max_priority` is an optional field on
`MessageServiceOptions`; `options` is the trailing argument on proxy methods
and on `publish_message`. Every existing call behaves identically.

The retry queue and the DLQ are deliberately left without `x-max-priority` of
their own, which keeps enabling this a **one**-queue migration rather than a
three-queue one. A retried message re-sorts correctly when it lands back in the
main priority queue, and it gets there with its priority intact for two separate
reasons that both had to be checked:

- The broker's dead-letter hop (retry queue → TTL expiry → DLX → main exchange)
  preserves the `priority` property. Verified against RabbitMQ 3.
- Protobus's own re-publish onto the retry exchange **copies `priority`
  explicitly**. This one is not free: protobus does not let the broker move a
  failed message, it re-publishes it and builds a fresh properties object by
  hand, so anything not copied is dropped. Without it, a control message that
  failed once would come back at priority 0 and queue behind the entire bulk
  backlog — the exact failure this feature exists to prevent, reachable only
  after something else has already gone wrong. The DLQ hop carries it too.

The general rule, and the one to remember when touching this code: **anywhere
protobus re-publishes rather than letting the broker move a message, priority
has to be carried by hand.**

## ⚠️ Enabling priority on a queue that already exists

**This is the one thing that is not backward compatible, and it cannot be made
so.** RabbitMQ fixes a queue's arguments when the queue is declared. Adding
`x-max-priority` to a queue that already exists without it is rejected:

```
Operation failed: QueueDeclare; 406 (PRECONDITION-FAILED) with message
"PRECONDITION_FAILED - inequivalent arg 'x-max-priority' for queue
'MyService' in vhost '/': received the value '2' of type 'byte' but current is none"
```

A 406 kills the channel the declare was issued on. Each listener opens its own
channel, so this is not a process-wide channel outage — but `init()` rejects and
**the service does not start**. Hitting it on a *reconnection* is no quieter:
the failed restore propagates to the connection, which discards that generation,
reports itself disconnected and retries, then gives up through its reconnection
budget. Either way it is loud. What it is not is recoverable without the
migration below.

So enabling `max_priority` on a service that has already run against a broker
requires a one-time, operator-driven **drain, delete and recreate** of that
service's main queue. Follow **Procedure A** in
[Queue Migration](../operations/queue-migration.md) — the same procedure as for a changed
`message_ttl_ms`, applied to `<service_name>` only (not `.Retry`, not `.DLQ`).

Deploying the new code before the queue is deleted fails loudly on startup with
the 406 above. That is the intended behaviour: a service refusing to start is
better than one that starts and quietly ignores the priorities you configured.

For a new service, declare `max_priority` from the first deploy and none of this
applies.

## Cross-language

`max_priority` / `priority` behave identically in the
[TypeScript port](https://github.com/ArielLaub/protobus) (`maxPriority`,
`priority`), including the validation ranges (1-255 and 0-255, integers only,
booleans rejected), what goes on the wire (no `priority` property when none was
asked for; an explicit `0` sent as `0`), and what each refuses (`max_priority`
with early ack). Verified against a live broker, both ports running at once:
a TypeScript publisher's `priority` is honoured by a Python consumer's
`max_priority` queue, and a TypeScript service redeclares a Python-created
priority queue without a 406 — the two emit the same queue arguments, which is
the one disagreement that would take a channel down.

Verified in **both** directions against a live broker with both ports running:

| Direction | Result |
|---|---|
| TS publisher → Python consumer (`max_priority=2`) | control message handled 2nd of 21 |
| Python publisher → TS consumer (`maxPriority: 2`) | control message handled 2nd of 21 |

> [!NOTE]
> protobus-py **1.x** differed from the TypeScript port in three internals
> (it always sent `priority: 0`, folded an explicit `0` to unset, and had no
> default prefetch). All three are gone in 2.0: the ports now emit the same
> bytes and reject the same configurations. See [Migration](../migration.md).

---

<div align="center">

**[← Streaming](./streaming.md)** · **[Docs index](../README.md)** · **[Testing →](./testing.md)**

</div>
