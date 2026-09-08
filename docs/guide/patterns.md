# Patterns

> Worked examples assembled from everything in the guide: concurrency, retries, events, service-to-service calls, shutdown and deployment.

**Read this if** you have a service running and want to know the shape of the next thing you are about to build.

| | |
|---|---|
| **Prerequisites** | [Getting Started](./getting-started.md) · [Error Handling](./error-handling.md) |
| **Next** | [Testing](./testing.md) · [Configuration](../reference/configuration.md) |
| **Source** | [`sample/combatGame`](../../sample/combatGame) — six services doing most of this at once |

**On this page** — [Concurrency](#concurrency-control) · [Retries](#retry-configuration) · [Events](#event-driven-patterns) · [Service to service](#service-to-service-calls) · [Shutdown](#graceful-shutdown-with-cleanup) · [Scaling out](#load-balancing-multiple-instances) · [Configuration](#environment-based-configuration) · [Docker](#docker-deployment) · [Resilience](#resilience-patterns)

> [!NOTE]
> Most snippets below are written against **CLI-generated typing**
> (`from types.proto import Service, CreateOrderRequest`), which is how a real
> project looks. The types come from your schema, not from this repository, so
> the snippets are illustrative; the [Getting Started](./getting-started.md)
> examples are executed by CI and are the place to copy runnable code from.

---

## Concurrency Control

By default, services process messages one at a time. For I/O-bound workloads, you can process multiple messages concurrently.

### Setting Max Concurrency

```python
import asyncio

from protobus import Context, RunnableService
from types.proto import ResizeRequest, ResizeResponse


class ImageProcessorService(RunnableService):
    service_name = "ImageProcessor.Service"

    async def resize(self, request: ResizeRequest, actor: str, correlation_id: str) -> ResizeResponse:
        # This can take 2-5 seconds per image
        result = await process_image(request["image_url"], request["width"], request["height"])
        return {"processed_url": result}


async def main() -> None:
    context = Context()
    await context.init("amqp://localhost", ["./proto"])

    # Process up to 10 images simultaneously
    await ImageProcessorService.start(context, max_concurrent=10)


asyncio.run(main())
```

`max_concurrent` is the consumer's prefetch: the broker hands the process that many unacknowledged messages at once, and each runs as its own task on the event loop.

### When to Use Concurrency

| Workload Type | Recommended Concurrency |
|---------------|------------------------|
| CPU-bound (image processing, encryption) | **1** — one process is one core; run more processes instead, or hand the work to a `ProcessPoolExecutor` |
| I/O-bound (database, HTTP calls) | 10-50+ depending on downstream capacity |
| Mixed | Start with 10, tune based on metrics |
| Sequential required (order processing) | 1 (default) |

> [!NOTE]
> asyncio concurrency is cooperative. A handler that blocks the loop — a synchronous HTTP client, a CPU-heavy loop — blocks every other in-flight message and the heartbeat with it. Use async libraries, or `await asyncio.to_thread(...)` for the blocking part.

### Parallelism Benefits Example

Without concurrency (sequential processing):
```
Request 1: [====2s====]
Request 2:             [====2s====]
Request 3:                         [====2s====]
Total: 6 seconds for 3 requests
```

With `max_concurrent=3`:
```
Request 1: [====2s====]
Request 2: [====2s====]
Request 3: [====2s====]
Total: 2 seconds for 3 requests
```

## Retry Configuration

Configure automatic retries for transient failures.

### Basic Retry Setup

```python
from protobus import RetryOptions

await MyService.start(
    context,
    retry=RetryOptions(
        max_retries=5,          # Retry up to 5 times
        retry_delay_ms=3000,    # Wait 3 seconds between retries
        message_ttl_ms=60000,   # Give up after 60 seconds total
    ),
)
```

### Retry Options

| Option | Default | Description |
|--------|---------|-------------|
| `max_retries` | `3` | Maximum retry attempts. Set to `0` to disable retries. |
| `retry_delay_ms` | `5000` | Delay between retries in milliseconds. |
| `message_ttl_ms` | `None` | Total message lifetime. Message is discarded after this time. |

### Preventing Retries for Specific Errors

Use `HandledError` for errors that should not be retried (validation errors, not found, etc.):

```python
from protobus import HandledError, RunnableService
from types.proto import GetOrderRequest, GetOrderResponse


class OrderService(RunnableService):
    service_name = "Orders.Service"

    async def getOrder(self, request: GetOrderRequest, actor: str, correlation_id: str) -> GetOrderResponse:
        order = await db.find_order(request["order_id"])

        if order is None:
            # This will NOT be retried - it's a handled business error
            raise HandledError("Order not found", "NOT_FOUND")

        # This WILL be retried if it fails
        enriched = await external_api.enrich_order(order)

        return {"order": enriched}
```

## Event-Driven Patterns

### Publishing Events

```python
class OrderService(RunnableService):
    service_name = "Orders.Service"

    async def createOrder(self, request: CreateOrderRequest, actor: str, correlation_id: str) -> CreateOrderResponse:
        order = await db.create_order(request)

        # Notify other services
        await self.publish_event("Orders.OrderCreated", {
            "order_id": order.id,
            "customer_id": order.customer_id,
            "total": order.total,
        })

        return {"order_id": order.id}
```

### Subscribing to Events

```python
class NotificationService(RunnableService):
    service_name = "Notifications.Service"

    async def init(self) -> None:
        await super().init()

        # Subscribe to order events
        await self.subscribe_event("Orders.OrderCreated", self.on_order_created)
        await self.subscribe_event("Orders.OrderShipped", self.on_order_shipped)

    async def on_order_created(self, event: dict) -> None:
        await self.send_email(event["customer_id"], "Your order has been created!")

    async def on_order_shipped(self, event: dict) -> None:
        await self.send_sms(event["customer_id"], f"Order {event['order_id']} shipped!")
```

### Topic-Based Routing

Use topics for fine-grained event routing:

```python
# Publisher: include region in topic
await self.publish_event("Orders.OrderCreated", order_data, f"orders.{order.region}.created")

# Subscriber: listen to specific region
await self.subscribe_event("Orders.OrderCreated", handler, "orders.US.*")

# Subscriber: listen to all regions
await self.subscribe_event("Orders.OrderCreated", handler, "orders.*.*")
```

## Service-to-Service Calls

### Calling Another Service

```python
from protobus import Context, HandledError, RunnableService, ServiceProxy
from types.proto import CheckoutRequest, CheckoutResponse, Inventory_Service, Payment_Service


class CheckoutService(RunnableService):
    service_name = "Checkout.Service"

    def __init__(self, context: Context):
        super().__init__(context)
        # Annotate with the generated Protocol and the type-checker knows the methods.
        self.inventory: Inventory_Service = ServiceProxy(context, "Inventory.Service")  # type: ignore[assignment]
        self.payment: Payment_Service = ServiceProxy(context, "Payment.Service")  # type: ignore[assignment]

    async def init(self) -> None:
        await super().init()
        await self.inventory.init()
        await self.payment.init()

    async def checkout(self, request: CheckoutRequest, actor: str, correlation_id: str) -> CheckoutResponse:
        # Check inventory
        inventory = await self.inventory.checkStock({"product_id": request["product_id"]})
        if not inventory["available"]:
            raise HandledError("Out of stock", "OUT_OF_STOCK")

        # Process payment
        payment = await self.payment.charge({
            "amount": request["amount"],
            "customer_id": request["customer_id"],
        })

        return {"order_id": payment["transaction_id"]}
```

The proxies share the service's context — one connection, one callback queue — so there is nothing extra to close.

## Graceful Shutdown with Cleanup

```python
class DatabaseService(RunnableService):
    service_name = "Database.Service"

    async def init(self) -> None:
        # Connect to the database before taking any messages
        self.db = await create_database_connection()
        await super().init()

    async def cleanup(self) -> None:
        # Called on SIGINT/SIGTERM, after in-flight messages have drained
        print("Closing database connection...")
        await self.db.close()
        print("Database connection closed")

    async def query(self, request: QueryRequest, actor: str, correlation_id: str) -> QueryResponse:
        rows = await self.db.query(request["sql"])
        return {"rows": rows}
```

The sequence on a signal is: stop consuming, drain in-flight messages (bounded by `SHUTDOWN_DRAIN_TIMEOUT_MS`), `cleanup()`, disconnect. A process that owns its own loop calls `await service.shutdown("reason")` for the same sequence; see [RunnableService](../reference/api/runnable-service.md).

## Load Balancing (Multiple Instances)

RabbitMQ automatically load balances across multiple service instances:

```bash
# Terminal 1
python -m services.calculator

# Terminal 2
python -m services.calculator

# Terminal 3
python -m services.calculator
```

All three declare the same `Calculator.Math` queue and compete for it; requests are distributed across the instances with no configuration. An instance that dies mid-request leaves its delivery unacked, and another instance picks it up.

## Environment-Based Configuration

```python
import asyncio
import os

from protobus import Context, RetryOptions, RunnableService


class MyService(RunnableService):
    service_name = "MyPackage.MyService"


async def main() -> None:
    context = Context()
    await context.init(
        os.environ.get("AMQP_URL", "amqp://localhost"),
        [os.environ.get("PROTO_PATH", "./proto")],
    )

    await MyService.start(
        context,
        max_concurrent=int(os.environ.get("MAX_CONCURRENT", "1")),
        retry=RetryOptions(
            max_retries=int(os.environ.get("MAX_RETRIES", "3")),
            retry_delay_ms=int(os.environ.get("RETRY_DELAY_MS", "5000")),
        ),
    )


asyncio.run(main())
```

The library's own timeouts are read from the environment already — `RPC_CALL_TIMEOUT_MS`, `MESSAGE_PROCESSING_TIMEOUT`, `PUBLISH_CONFIRM_TIMEOUT_MS` and the rest — see [Configuration](../reference/configuration.md).

## Docker Deployment

```dockerfile
# Dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY services/ ./services/
COPY proto/ ./proto/
RUN pip install --no-cache-dir .

ENV AMQP_URL=amqp://rabbitmq:5672
ENV PROTO_PATH=./proto
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "services.calculator"]
```

```yaml
# docker-compose.yml
services:
  rabbitmq:
    image: rabbitmq:3-management
    ports:
      - "5672:5672"
      - "15672:15672"

  calculator:
    build: .
    environment:
      AMQP_URL: amqp://rabbitmq:5672
      MAX_CONCURRENT: "10"
    depends_on:
      - rabbitmq
    deploy:
      replicas: 3  # Run 3 instances for load balancing
```

`RunnableService.start` handles `SIGTERM`, so `docker stop` gets a drained, clean shutdown rather than a kill — as long as the Python process is PID 1 or receives the signal, which the exec-form `CMD` above guarantees.

---

## Resilience patterns

These are ordinary application patterns rather than protobus features, and they
live here so the [Error Handling](./error-handling.md) page can stay about what
protobus actually does. Use them around a `ServiceProxy` call the same way you
would around any remote call.

### Retry a call from the caller's side

Protobus retries on the **server**. A caller that wants its own attempts — for a
timeout, or a service that was briefly not running — needs its own loop, and must
not retry a terminal failure:

```python
import asyncio
from typing import Awaitable, Callable, TypeVar

from protobus import RemoteError, is_handled_error

T = TypeVar("T")

TERMINAL_CODES = {"VALIDATION_ERROR", "NOT_FOUND"}


async def call_with_retry(fn: Callable[[], Awaitable[T]], max_attempts: int = 3, backoff_ms: int = 1000) -> T:
    last_error: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except RemoteError as error:
            last_error = error
            # A code the service set deliberately means the same request
            # fails the same way. Stop.
            if error.code in TERMINAL_CODES:
                raise
        except Exception as error:
            last_error = error
            if is_handled_error(error):
                raise
        if attempt < max_attempts:
            await asyncio.sleep(backoff_ms * 2 ** (attempt - 1) / 1000)

    assert last_error is not None
    raise last_error
```

> [!WARNING]
> A caller-side retry stacks on top of the server-side ladder. With the defaults
> a single `call_with_retry` of three attempts can mean twelve handler invocations
> and roughly 45 seconds. Decide which layer owns the retry; rarely both.

### Circuit breaker

```python
import time
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.failures = 0
        self.opened_at = 0.0

    async def run(self, fn: Callable[[], Awaitable[T]]) -> T:
        if self.failures >= self.threshold:
            if time.monotonic() - self.opened_at < self.cooldown_s:
                raise CircuitOpen("circuit open")
            self.failures = 0  # half-open: let one through
        try:
            result = await fn()
        except Exception:
            self.failures += 1
            self.opened_at = time.monotonic()
            raise
        self.failures = 0
        return result
```

### Graceful degradation

When a dependency is optional, answer without it rather than failing the whole
request — but say so in the response, so the caller can tell a real answer from a
degraded one:

```python
from typing import Awaitable, Callable


async def build_profile(id: str, fetch_recommendations: Callable[[str], Awaitable[list[str]]]) -> dict:
    try:
        return {"id": id, "recommendations": await fetch_recommendations(id), "degraded": False}
    except Exception:
        return {"id": id, "recommendations": [], "degraded": True}
```

### Input validation at the edge

Validate before doing any work, and raise `HandledError` so the message is
answered rather than retried four times:

```python
from protobus import HandledError


def require_fields(request: dict, fields: list[str]) -> None:
    missing = [f for f in fields if request.get(f) is None]
    if missing:
        raise HandledError(f"missing required field(s): {', '.join(missing)}", "VALIDATION_ERROR")
```

Remember that proto3 scalars decode to their zero value when unset — `""`, `0`,
`False` — so `is None` catches only absent messages, `optional` fields and
`oneof` members. For a required string, test `if not request.get("customer_id")`.

---

<div align="center">

**[← Testing](./testing.md)** · **[Docs index](../README.md)** · **[CLI →](../reference/cli.md)**

</div>
