# Events API

Protobus provides a publish-subscribe event system with topic-based routing and wildcard support.

## Overview

Events are published to the events exchange and delivered to all matching subscribers. Unlike RPC, events are fire-and-forget with no response.

## Publishing Events

### From MessageService

The simplest way to publish events is from a `MessageService`:

```python
from protobus import MessageService

class OrderService(MessageService):
    async def create_order(self, data, actor, correlation_id):
        order = self._create_order(data)

        # Publish event
        await self.publish_event("OrderCreated", {
            "order_id": order["id"],
            "customer": data["customer"],
            "total": order["total"]
        })

        return order
```

### From Context

You can also publish events directly from the context:

```python
await ctx.publish_event("SystemStarted", {
    "version": "1.0.0",
    "timestamp": time.time()
})
```

### Custom Topics

By default, events are routed to `EVENT.<event_type>`. You can specify custom topics:

```python
# Default routing
await self.publish_event("OrderCreated", data)
# Routes to: EVENT.OrderCreated

# Custom topic for regional routing
await self.publish_event("OrderCreated", data, topic="EVENT.Orders.US.Created")
# Routes to: EVENT.Orders.US.Created
```

## Subscribing to Events

### From MessageService

Subscribe to events in your service:

```python
class NotificationService(MessageService):
    async def init(self):
        await super().init()

        # Subscribe to specific event type
        await self.subscribe_event("OrderCreated", self.on_order_created)

        # Subscribe with custom topic pattern
        await self.subscribe_event(
            "OrderShipped",
            self.on_order_shipped,
            topic="EVENT.Orders.*.Shipped"
        )

    async def on_order_created(self, data: dict, topic: str) -> None:
        """Handle order created events."""
        print(f"New order: {data['order_id']}")

    async def on_order_shipped(self, data: dict, topic: str) -> None:
        """Handle order shipped events (any region)."""
        print(f"Order shipped: {data['order_id']} from {topic}")
```

### EventHandler Signature

```python
async def handler(data: Any, topic: str) -> None
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `data` | `Any` | Event payload |
| `topic` | `str` | Full routing topic |

## Topic Patterns

### Wildcards

| Pattern | Matches | Example |
|---------|---------|---------|
| `*` | Single word | `EVENT.Order.*` matches `EVENT.Order.Created` |
| `#` | Zero or more words | `EVENT.Order.#` matches `EVENT.Order.US.Created` |

### Pattern Examples

```python
# Exact match
await self.subscribe_event("OrderCreated", handler)
# Matches: EVENT.OrderCreated

# Single-level wildcard
await self.subscribe_event("any", handler, topic="EVENT.Order.*")
# Matches: EVENT.Order.Created, EVENT.Order.Shipped
# Not: EVENT.Order.US.Created

# Multi-level wildcard
await self.subscribe_event("any", handler, topic="EVENT.Order.#")
# Matches: EVENT.Order.Created, EVENT.Order.US.Created, EVENT.Order.EU.Shipped

# Combined patterns
await self.subscribe_event("any", handler, topic="EVENT.*.US.*")
# Matches: EVENT.Order.US.Created, EVENT.User.US.Registered
```

## EventListener Class

For advanced use cases, use `EventListener` directly:

```python
from protobus import EventListener

async def main():
    ctx = Context()
    await ctx.init("amqp://...")

    # Create listener
    listener = EventListener(ctx.connection, ctx.factory)
    await listener.init(None, "my-events-queue")

    # Subscribe to events
    async def handler(data, topic):
        print(f"Event on {topic}: {data}")

    await listener.subscribe("OrderCreated", handler)
    await listener.subscribe("UserRegistered", handler)

    # Start listening
    await listener.start()
```

### Subscribe All Events

Catch all events for debugging or logging:

```python
async def catch_all(data, topic):
    print(f"[{topic}] {data}")

await listener.subscribe_all(catch_all)
```

## Best Practices

### 1. Use Semantic Event Names

```python
# Good - describes what happened
await self.publish_event("OrderCreated", data)
await self.publish_event("PaymentProcessed", data)
await self.publish_event("UserRegistered", data)

# Bad - describes action, not event
await self.publish_event("CreateOrder", data)
await self.publish_event("ProcessPayment", data)
```

### 2. Include Relevant Data

```python
# Good - includes useful context
await self.publish_event("OrderCreated", {
    "order_id": order.id,
    "customer_id": order.customer_id,
    "total": order.total,
    "created_at": order.created_at.isoformat()
})

# Bad - forces subscribers to make additional calls
await self.publish_event("OrderCreated", {
    "order_id": order.id
})
```

### 3. Handle Errors Gracefully

```python
async def on_event(self, data, topic):
    try:
        await self._process_event(data)
    except Exception as e:
        # Log but don't re-raise to prevent message requeue
        print(f"Error processing {topic}: {e}")
        # Optionally publish error event
        await self.publish_event("EventProcessingFailed", {
            "original_topic": topic,
            "error": str(e)
        })
```

### 4. Use Topics for Routing

```python
# Regional routing
await self.publish_event("OrderCreated", data, topic=f"EVENT.Orders.{region}.Created")

# Priority routing
await self.publish_event("Alert", data, topic=f"EVENT.Alerts.{priority}")

# Service-specific routing
await self.publish_event("Notification", data, topic=f"EVENT.{service_name}.Notification")
```

## Example: Event-Driven Architecture

```python
# Order Service - publishes events
class OrderService(MessageService):
    async def create_order(self, data, actor, correlation_id):
        order = await self._save_order(data)
        await self.publish_event("OrderCreated", {
            "order_id": order["id"],
            "items": data["items"],
            "total": order["total"]
        })
        return order


# Inventory Service - subscribes to order events
class InventoryService(MessageService):
    async def init(self):
        await super().init()
        await self.subscribe_event("OrderCreated", self.on_order_created)

    async def on_order_created(self, data, topic):
        for item in data["items"]:
            await self._reserve_inventory(item)
        await self.publish_event("InventoryReserved", {
            "order_id": data["order_id"]
        })


# Notification Service - subscribes to multiple events
class NotificationService(MessageService):
    async def init(self):
        await super().init()
        await self.subscribe_event("OrderCreated", self.notify_order_created)
        await self.subscribe_event("InventoryReserved", self.notify_inventory)
        await self.subscribe_event("OrderShipped", self.notify_shipped)

    async def notify_order_created(self, data, topic):
        await self._send_email("order_confirmation", data["order_id"])

    async def notify_inventory(self, data, topic):
        print(f"Inventory reserved for order {data['order_id']}")

    async def notify_shipped(self, data, topic):
        await self._send_email("shipping_notification", data["order_id"])
```

---

See also: [MessageService](message-service.md) | [Message Flow](../message-flow.md)
