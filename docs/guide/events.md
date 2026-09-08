# Events

> Publish/subscribe on the bus: emitting an event, receiving one, and the four things about it that catch people out.

**Read this if** you want one service to tell others that something happened without waiting for them to deal with it.

| | |
|---|---|
| **Prerequisites** | [Getting Started](./getting-started.md) — a service that runs · [Schema](./schema.md) |
| **Next** | [Error Handling](./error-handling.md) · [Message Flow](../concepts/message-flow.md) — the event on the wire |
| **Source** | [`protobus/event_dispatcher.py`](../../protobus/event_dispatcher.py) · [`protobus/event_listener.py`](../../protobus/event_listener.py) · [`protobus/message_service.py`](../../protobus/message_service.py) · [`protobus/trie.py`](../../protobus/trie.py) |

**On this page** — [The shape of it](#the-shape-of-it) · [A subscriber needs a service block](#a-subscriber-still-needs-a-service-block) · [Publishing](#publishing) · [Subscribing](#subscribing) · [Topics route, types do not](#topics-route-types-do-not) · [Wildcards](#wildcard-patterns) · [Several handlers](#several-handlers-one-topic) · [When a handler raises](#when-a-handler-raises) · [Turning retry on](#retry) · [What survives what](#what-survives-what) · [A subscriber that is not a service](#a-subscriber-that-is-not-a-service) · [Worked example](#worked-example)

---

## The shape of it

An event is one-way. The publisher does not know who is listening, does not wait, and is never told whether anyone processed it.

```mermaid
flowchart LR
    S["OrderService<br/>publish_event"] --> X{{"proto.bus.events<br/>topic exchange"}}
    X -->|"EVENT.Orders.OrderCreated"| Q1[("Notifications.Service.Events")]
    X -->|"EVENT.Orders.OrderCreated"| Q2[("Analytics.Service.Events")]
    X -->|"no matching binding"| N["dropped by the broker"]
    Q1 --> H1["handler"]
    Q2 --> H2["handler"]

    style N fill:#9a6700,color:#fff,stroke:#9a6700
```

Each subscribing **service** has one durable queue named `<service_name>.Events`, and its replicas compete for it — an event is handled once per service, not once per replica. Events published while every replica of a service is down are waiting in that queue when one comes back.

> [!NOTE]
> Events are published without AMQP's `mandatory` flag, deliberately. An event nobody has subscribed to is discarded by the broker in silence, and that is normal rather than an error. Publishing an event proves nothing about it having been received.

---

## A subscriber still needs a `service` block

This is the first thing that goes wrong.

Protobus resolves a class's contract by looking `service_name` up in the loaded schema — `_resolve_contract` in [`protobus/message_service.py`](../../protobus/message_service.py) trims segments from the right until one names a `service`. A class with no matching `service` anywhere raises at `init()`:

```
MissingProto: no service in the schema matches 'Notifications.Service' or any
prefix of it; the .proto must declare the service this class serves
```

That holds even when the class implements no RPCs at all and only ever subscribes. An empty block is enough:

```protobuf
syntax = "proto3";
package Notifications;

// No rpcs. It exists so _resolve_contract can find 'Notifications.Service'.
service Service {
}
```

and the class alongside it:

```python
from protobus import RunnableService


class NotificationService(RunnableService):
    service_name = "Notifications.Service"

    async def init(self) -> None:
        # Must come first. subscribe_event binds a queue that does not exist
        # until MessageService.init() has declared it.
        await super().init()

        await self.subscribe_event("Orders.OrderCreated", self.on_order_created)

    async def on_order_created(self, event: dict, event_type: str, topic: str) -> None:
        print(f"order {event['order_id']} for user {event['user_id']}")
```

> [!IMPORTANT]
> **Order matters.** `subscribe_event` binds a routing key on the listener's queue and channel, both of which are created by `MessageService.init()`. Calling it before `super().init()` raises.
>
> `RunnableService.launch()` and `start()` also accept a `post_init` coroutine, called after `init()` — a place to subscribe without overriding `init` at all. [Getting Started → Subscribe to events](./getting-started.md#6-subscribe-to-events) does it that way.

---

## Publishing

`publish_event` is a method on `MessageService`, so it is available anywhere inside a service; `Context.publish_event` is the same call for a process that is not one.

```
async publish_event(event_type: str, content: Any, topic: str | None = None) -> None
```

| Argument | Meaning |
|---|---|
| `event_type` | the fully qualified **message** type — `<Package>.<MessageType>`. It must be a message in the loaded schema; it is not part of any `service` block. |
| `content` | a `dict` matching that message. Field names follow the `.proto` exactly, so `order_id` stays `order_id`. |
| `topic` | the routing key. Omit it and it defaults to `EVENT.<event_type>`. |

```python
from protobus import RunnableService


class OrderService(RunnableService):
    service_name = "Orders.Service"

    async def createOrder(self, request: dict, actor: str, correlation_id: str) -> dict:
        order_id = "ord-123"

        # Default topic: EVENT.Orders.OrderCreated
        await self.publish_event("Orders.OrderCreated", {
            "order_id": order_id,
            "user_id": request["user_id"],
        })

        return {"order_id": order_id}

    async def shipOrder(self, request: dict, actor: str, correlation_id: str) -> dict:
        # Custom topic, so subscribers can filter by region without decoding.
        await self.publish_event(
            "Orders.OrderShipped",
            {"order_id": request["order_id"]},
            f"ORDERS.{request['region']}.SHIPPED",
        )

        return {"ok": True}
```

The call returns once the broker has confirmed the message. It does **not** wait for any subscriber. A failure to encode the event raises `InvalidMessageError` and the payload never reaches the log.

> [!TIP]
> Put everything a subscriber needs *in* the event. A subscriber that has to call back to ask "and what were the line items?" has turned an event into a slower RPC, and couples the two services in the direction the event was meant to decouple.

---

## Subscribing

```
async subscribe_event(event_type: str, handler: EventHandler, topic: str | None = None) -> Any
```

The handler takes **three** arguments, not one:

```python
from protobus import EventHandler  # Callable[..., Awaitable[None]]


async def on_shipped(event: dict, event_type: str, topic: str) -> None:
    print(event_type, topic, event)
```

| Argument | What it is |
|---|---|
| `event` | the decoded payload |
| `type` | the event type as carried in the envelope, e.g. `Orders.OrderShipped` |
| `topic` | the topic **from the envelope body**, which is not always the routing key the delivery matched |

A handler may declare fewer parameters and is called with what it declares — `(event)` or `(event, topic)`. Note that the two-parameter form receives the **topic**, not the type; a handler with `*args` is treated as three-parameter.

```python
from protobus import RunnableService


class AnalyticsService(RunnableService):
    service_name = "Analytics.Service"

    async def init(self) -> None:
        await super().init()

        # Default topic: binds EVENT.Orders.OrderCreated.
        async def on_created(event: dict) -> None:
            print("created", event["order_id"])

        await self.subscribe_event("Orders.OrderCreated", on_created)

        # Explicit topic with a wildcard: binds ORDERS.*.SHIPPED.
        async def on_shipped(event: dict, event_type: str, topic: str) -> None:
            print(f"{event_type} on {topic}: {event['order_id']}")

        await self.subscribe_event("Orders.OrderShipped", on_shipped, "ORDERS.*.SHIPPED")
```

Each call does two things: it binds `topic` on this service's `.Events` queue, and it registers the handler under `topic` in an in-process [`Trie`](../../protobus/trie.py). The binding decides which messages reach the process; the trie decides which handlers run.

---

## Topics route, types do not

The single most misleading thing about the API is that `type` looks like a filter. It is not.

> [!WARNING]
> **When you pass a `topic`, the `event_type` argument to `subscribe_event` is ignored for routing.** It is used only to compute the default topic when you omit one ([`protobus/event_listener.py`](../../protobus/event_listener.py), `subscribe`). Nothing anywhere compares an arriving event's type against the type you subscribed with. `subscribe_event('Orders.OrderShipped', h, 'ORDERS.#')` runs `h` for **every** event published under a topic beginning `ORDERS.` — including `Orders.OrderCancelled`, and including a type from another team's package.

Two consequences worth designing around:

- **Guard on `event_type` inside a broad handler**, or give each event type a topic prefix that no other type shares.
- **A wildcard subscriber must have every type it can receive in its own schema.** The listener decodes with the type carried in the envelope, so an unknown type makes `decode_event` raise — and that is a handler failure, with the consequences in [When a handler raises](#when-a-handler-raises).

There is a matching asymmetry on the two `topic` values in play:

| | Value |
|---|---|
| the delivery matched on | the AMQP routing key — what the trie matches, and what the broker used |
| the handler's 3rd argument | the `topic` field inside the envelope body |

They agree for anything published by protobus. The listener prefers the routing key precisely because the body does not have to: it is publisher-controlled, and trusting it would let a publisher reach handlers its routing key was never permitted to reach.

---

## Wildcard patterns

The grammar is RabbitMQ's, and the matching is protobus's own trie.

| Token | Matches |
|---|---|
| `*` | exactly one word |
| `#` | zero or more words |

Words are separated by `.`. Every row below was executed against the real matcher:

| Pattern | Matches | Does not match |
|---|---|---|
| `ORDERS.*` | `ORDERS.US`, `ORDERS.EU` | `ORDERS.US.CA` |
| `ORDERS.*.SHIPPED` | `ORDERS.US.SHIPPED`, `ORDERS.EU.SHIPPED` | `ORDERS.SHIPPED`, `ORDERS.US.CA.SHIPPED` |
| `ORDERS.#` | `ORDERS`, `ORDERS.US`, `ORDERS.US.CA.SHIPPED` | `SALES.US` |
| `ORDERS.#.SHIPPED` | `ORDERS.SHIPPED`, `ORDERS.US.SHIPPED`, `ORDERS.US.CA.SHIPPED` | `ORDERS.US` |

> [!WARNING]
> `*` is **exactly one** word, never "one or more". `ORDERS.*.SHIPPED` reads as "any shipped order" but describes a strictly three-word topic, so a four-word `ORDERS.US.CA.SHIPPED` does not match it. Use `#` wherever the number of words can vary. The worked example in [Message Flow](../concepts/message-flow.md#wildcard-matching) is pinned by a unit test for exactly this reason.

Design topics so the varying part is one segment, and put the stable discriminator at a fixed position:

```
ORDERS.<region>.<status>      ORDERS.*.SHIPPED works, ORDERS.US.# works
ORDERS.<status>.<region>      you now need two patterns to say "shipped"
```

---

## Several handlers, one topic

Subscribing twice to the same topic registers both handlers. Both run, in registration order, and each is awaited before the next starts.

```python
from protobus import RunnableService


class ReportingService(RunnableService):
    service_name = "Reporting.Service"

    async def init(self) -> None:
        await super().init()

        await self.subscribe_event("Orders.OrderCreated", self.store)

        # Same topic. Both handlers run for every delivery.
        await self.subscribe_event("Orders.OrderCreated", self.count_it)

    async def store(self, event: dict) -> None: ...
    async def count_it(self, event: dict) -> None: ...
```

> [!CAUTION]
> They are **not** independent. The handlers share one delivery and one acknowledgement, and they are awaited in a plain loop — so if the first raises, the second never runs and the whole delivery is lost. Independent side effects that must not take each other down belong in separate services with separate queues.

Two more limits on this shape:

- Events are processed **one at a time per process**. The event listener uses late acknowledgement with a prefetch of `DEFAULT_PREFETCH`, which is **1**. `max_concurrent` on `MessageServiceOptions` is passed only to the RPC listener, so raising it does not widen the event path.
- There is no `unsubscribe`. The trie has no remove.

---

## When a handler raises

This is the section to read before you rely on events for anything that must not be lost.

```mermaid
flowchart TD
    D[("Orders.Service.Events delivers")] --> H["run every matching handler"]
    H --> OK{"did they all resolve?"}
    OK -->|yes| A["ack — done"]
    OK -->|"no, one raised"| R["reject, requeue = false"]
    R --> G["the event is discarded"]

    style G fill:#a40e26,color:#fff,stroke:#a40e26
```

> [!CAUTION]
> **By default a failed event handler does not retry, and there is no event DLQ.** `MessageListener` declares `<Service>.Retry`, `<Service>.Retry.Exchange` and `<Service>.DLQ` for the RPC queue. `EventListener` declares none of them unless you ask, so the connection layer takes its no-retry branch: the delivery is rejected without requeue and the message is gone. There is also no caller to reply to, so nothing anywhere records that it happened beyond one `rejecting message` line in the log.
>
> Rejecting is what keeps the subscriber alive — an unacknowledged delivery would hold the prefetch and stall everything behind the first permanently-failing event. [Turning retry on](#turning-retry-on) replaces that trade rather than removing it.

Corollaries:

- **`HandledError` changes nothing on the event path by default.** On the RPC path it is the difference between an immediate error reply and three retries. Here both branches end in the same reject-without-requeue. Raising it is harmless and communicates intent, but it does not prevent a retry, because there is no retry.
- **"Unacknowledged events are redelivered" is true only for a crash.** Late acknowledgement means an event whose process dies mid-handler is unacked and comes back. An event whose handler *raised* is settled, not unacked.

So the durability of an event's *effects* is yours to arrange. Either catch inside the handler and record the failure somewhere you can replay from:

```python
from protobus import RunnableService


class BillingService(RunnableService):
    service_name = "Billing.Service"

    async def init(self) -> None:
        await super().init()
        await self.subscribe_event("Orders.OrderCreated", self.on_order_created)

    async def on_order_created(self, event: dict) -> None:
        try:
            await self.charge(event)
        except Exception as error:
            # Nothing above this line will retry, so failure has to be
            # recorded here or it is recorded nowhere.
            await self.park_for_replay(event, error)

    async def charge(self, event: dict) -> None: ...
    async def park_for_replay(self, event: dict, error: Exception) -> None: ...
```

or, when the work genuinely must not be lost, do not model it as an event at all. An RPC has the retry ladder and the DLQ — see [Delivery Guarantees](../concepts/delivery-guarantees.md).

<a id="retry"></a>
### Turning retry on

`event_retry` gives a service's event subscriptions the ladder its RPC queue
already has. It is off by default, because switching it on declares queues and
exchanges the service did not have before and changes what a handler failure
means for every subscription on that service.

```python
from protobus import Context, EventRetryOptions, MessageServiceOptions, RunnableService


class BillingService(RunnableService):
    service_name = "Billing.Service"

    def __init__(self, context: Context):
        # Four handler runs per event, 2s apart, then the DLQ.
        super().__init__(context, MessageServiceOptions(event_retry=EventRetryOptions(max_retries=3, retry_delay_ms=2000)))
```

It declares four objects alongside `<Service>.Events`:

| Object | Purpose |
|---|---|
| `<Service>.Events.Retry` | parks the failed event for `retry_delay_ms`, then dead-letters it |
| `<Service>.Events.Retry.Exchange` | topic exchange the failed event is published to, so its routing key survives the hop |
| `<Service>.Events.Redelivery` | topic exchange bound only to `<Service>.Events` — where the expired event comes back |
| `<Service>.Events.DLQ` | where an event lands once `max_retries` hops are spent |

The redelivery exchange is the part worth understanding. A request's retry
dead-letters back to `proto.bus`, which routes to one service queue. Events fan
out, so dead-lettering back to `proto.bus.events` would redeliver to *every*
subscriber bound to that topic, including the ones that succeeded. The
per-subscriber exchange confines the retry to the service that failed.

Three things to know before switching it on:

- **A retry re-runs every handler that matched, not just the one that raised.**
  Handlers sharing a topic share one delivery ([Several handlers, one
  topic](#several-handlers-one-topic)), so a handler that already succeeded runs
  again. It must be idempotent, keyed on the event's `message_id`, which is
  preserved across every hop.
- **`retry_delay_ms` becomes the retry queue's `x-message-ttl`** and cannot be
  changed on a service that has already run, exactly as on the RPC path. A
  changed value fails startup with `RetryQueueMismatchError`. See
  [Queue Migration](../operations/queue-migration.md).
- **`HandledError` starts meaning something.** With retry off it is inert on the
  event path; with retry on it is what says "do not retry this", and the event
  goes straight to the DLQ.

Verified against a real broker in
[`tests/integration/test_events_and_dlq.py`](../../tests/integration/test_events_and_dlq.py).

---

## What survives what

| Failure | Event in flight |
|---|---|
| Broker restarts | **survives** — published persistent (`delivery_mode` 2), and `<Service>.Events` is durable |
| Every replica of a subscriber is down | **survives** — the queue is durable and not auto-delete, so it accumulates |
| A replica is killed mid-handler | **redelivered** — late ack, so the delivery was never settled |
| The handler raises | **lost** by default — rejected without requeue. With [`event_retry`](#retry): retried, then dead-lettered |
| Nobody has ever subscribed | **lost** — no binding matches, and events are not published `mandatory` |

> [!WARNING]
> The second row is a real operational hazard in the other direction. `<Service>.Events` is durable and never auto-deletes, so the event queue of a service you deleted keeps filling forever. See [Queue Migration](../operations/queue-migration.md).

---

## A subscriber that is not a service

A process that only listens does not need `RunnableService.start` and its signal handling; it can own its own lifecycle. It still needs the same `service` block, and it must close its connection.

```python
import asyncio
import os
import signal

from protobus import Context


async def main() -> None:
    context = Context()
    await context.init(
        os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/"),
        [os.path.join(os.path.dirname(__file__), "proto")],
    )

    subscriber = NotificationService(context)
    await subscriber.init()

    async def on_shipped(event: dict) -> None:
        print("shipped", event["order_id"])

    await subscriber.subscribe_event("Orders.OrderShipped", on_shipped, "ORDERS.*.SHIPPED")
    print("listening")

    # A long-lived listener stays here. A short-lived script must close the
    # context, or the open socket keeps the loop alive.
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)
    await stop.wait()
    await subscriber.close()
    await context.close()


asyncio.run(main())
```

> [!IMPORTANT]
> **Close the context.** `await context.close()` releases the dispatchers and disconnects. Without it an open AMQP socket and its heartbeat keep the event loop alive and `asyncio.run` never returns. `RunnableService.start` does this for you on SIGINT/SIGTERM; a script you wrote yourself does not.

> [!NOTE]
> If you also want to *call* a service from the same process, `ServiceProxy` builds its methods from the schema at `init()`, so a type-checker cannot know them. `protobus generate` writes a `typing.Protocol` per service; annotate the proxy with it and `proxy.someMethod(...)` is checked at every call site.

---

## Worked example

One publisher, two independent subscribers, and the schema all three share.

```protobuf
syntax = "proto3";
package Orders;

message CreateOrderRequest {
    string user_id = 1;
    repeated string skus = 2;
}

message CreateOrderResponse {
    string order_id = 1;
}

message ShipOrderRequest {
    string order_id = 1;
    string region = 2;
    string carrier = 3;
}

message ShipOrderResponse {
    bool ok = 1;
}

message OrderCreated {
    string order_id = 1;
    string user_id = 2;
    int64  created_at = 3;
    repeated string skus = 4;
}

message OrderShipped {
    string order_id = 1;
    string carrier = 2;
    string region = 3;
}

service Service {
    rpc createOrder(Orders.CreateOrderRequest) returns(Orders.CreateOrderResponse);
    rpc shipOrder(Orders.ShipOrderRequest) returns(Orders.ShipOrderResponse);
}
```

```python
import time

from protobus import RunnableService


class OrdersService(RunnableService):
    service_name = "Orders.Service"

    async def createOrder(self, request: dict, actor: str, correlation_id: str) -> dict:
        order_id = "ord-123"

        # Everything a subscriber could want, in the event itself.
        await self.publish_event("Orders.OrderCreated", {
            "order_id": order_id,
            "user_id": request["user_id"],
            "created_at": int(time.time() * 1000),
            "skus": request["skus"],
        })

        return {"order_id": order_id}

    async def shipOrder(self, request: dict, actor: str, correlation_id: str) -> dict:
        # The region goes in the TOPIC, so a subscriber can filter on it
        # without decoding anything.
        await self.publish_event("Orders.OrderShipped", {
            "order_id": request["order_id"],
            "carrier": request["carrier"],
            "region": request["region"],
        }, f"ORDERS.{request['region']}.SHIPPED")

        return {"ok": True}


class EmailService(RunnableService):
    service_name = "Email.Service"

    async def init(self) -> None:
        await super().init()
        await self.subscribe_event("Orders.OrderCreated", self.on_order_created)

    async def on_order_created(self, event: dict) -> None:
        try:
            await self.send(event["user_id"], event["order_id"])
        except Exception as error:
            # No retry exists above this line.
            print("email failed for", event["order_id"], error)

    async def send(self, user_id: str, order_id: str) -> None: ...


class ShipmentTracker(RunnableService):
    service_name = "Tracking.Service"

    async def init(self) -> None:
        await super().init()
        # Region-scoped: ORDERS.US.SHIPPED matches, ORDERS.EU.SHIPPED does not.
        await self.subscribe_event("Orders.OrderShipped", self.on_shipped, "ORDERS.US.SHIPPED")

    async def on_shipped(self, event: dict, event_type: str, topic: str) -> None:
        # The topic decides which handler runs; the type never does.
        if event_type != "Orders.OrderShipped":
            return
        print("US shipment", event["order_id"], event["carrier"])
```

`EmailService` and `ShipmentTracker` each get their own durable queue, so one being down does not affect the other, and neither affects `OrdersService`.

---

<div align="center">

**[← Schema](./schema.md)** · **[Docs index](../README.md)** · **[Error Handling →](./error-handling.md)**

</div>
