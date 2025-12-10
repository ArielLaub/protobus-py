# ServiceCluster API

The `ServiceCluster` class manages multiple service instances in a single process. It's useful for running multiple services together and for horizontal scaling within a process.

## Import

```python
from protobus import ServiceCluster
```

## Constructor

```python
ServiceCluster(context: IContext)
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `context` | `IContext` | Shared context for all services |

## Properties

### service_names

```python
@property
def service_names(self) -> List[str]
```

Returns a list of all registered service names.

Alias: `ServiceNames` (for TypeScript API compatibility)

## Methods

### use

```python
def use(self, service_class: Type[T], count: int = 1) -> T
```

Register a service class with the cluster.

| Parameter | Type | Description |
|-----------|------|-------------|
| `service_class` | `Type[MessageService]` | The service class to instantiate |
| `count` | `int` | Number of instances to create (default: 1) |

**Returns:** The first service instance (for accessing methods/properties).

**Example:**

```python
cluster = ServiceCluster(ctx)
calculator = cluster.use(CalculatorService)  # Single instance
users = cluster.use(UserService, count=3)    # 3 instances for load balancing
```

### init

```python
async def init(self) -> None
```

Initialize all registered services. Services are initialized in registration order.

**Example:**

```python
await cluster.init()
print(f"Running: {cluster.service_names}")
```

## Example: Basic Usage

```python
from protobus import Context, ServiceCluster, MessageService

class CalculatorService(MessageService):
    @property
    def service_name(self) -> str:
        return "calculator.MathService"

    @property
    def proto_file_name(self) -> str:
        return ""

    async def add(self, data, actor, correlation_id):
        return {"result": data["a"] + data["b"]}


class NotificationService(MessageService):
    @property
    def service_name(self) -> str:
        return "notifications.NotifyService"

    @property
    def proto_file_name(self) -> str:
        return ""

    async def send(self, data, actor, correlation_id):
        print(f"Sending: {data['message']}")
        return {"sent": True}


async def main():
    ctx = Context()
    await ctx.init("amqp://guest:guest@localhost:5672/")

    cluster = ServiceCluster(ctx)

    # Register services
    calc = cluster.use(CalculatorService)
    notify = cluster.use(NotificationService)

    # Initialize all
    await cluster.init()

    print(f"Services running: {cluster.service_names}")
    # Output: Services running: ['calculator.MathService', 'notifications.NotifyService']

    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        await ctx.close()
```

## Example: Load Balancing

Create multiple instances for parallel processing:

```python
cluster = ServiceCluster(ctx)

# Create 5 worker instances
# RabbitMQ will distribute messages across them
cluster.use(WorkerService, count=5)

await cluster.init()
```

**How it works:**
- All instances share the same queue name
- RabbitMQ distributes messages round-robin
- `max_concurrent` per instance limits parallel processing

## Example: Mixed Services

```python
async def main():
    ctx = Context()
    await ctx.init("amqp://...")

    cluster = ServiceCluster(ctx)

    # API service - single instance
    api = cluster.use(APIService, count=1)

    # Worker service - multiple instances for throughput
    workers = cluster.use(WorkerService, count=4)

    # Notification service - single instance
    notify = cluster.use(NotificationService, count=1)

    await cluster.init()

    print(f"Running {len(cluster.service_names)} service types")
    # Note: 6 total instances (1 + 4 + 1), but 3 unique service names
```

## Example: Accessing Service Methods

The `use()` method returns the first instance, which you can use to call methods:

```python
cluster = ServiceCluster(ctx)

# Get reference to first instance
calc = cluster.use(CalculatorService)
await cluster.init()

# Use the service directly (local call, not via RabbitMQ)
result = await calc.add({"a": 1, "b": 2}, "", "")
print(result)  # {"result": 3}

# Publish events through the service
await calc.publish_event("ServiceStarted", {"name": calc.service_name})
```

## Example: With Service Options

Service options are passed to each constructor:

```python
from protobus import MessageServiceOptions, RetryOptions

class MyService(MessageService):
    def __init__(self, ctx, options=None):
        # Custom options
        default_options = MessageServiceOptions(
            max_concurrent=10,
            retry=RetryOptions(max_retries=5)
        )
        super().__init__(ctx, options or default_options)

# In cluster
cluster.use(MyService)  # Uses default options from constructor
```

## Best Practices

### 1. Match Instance Count to Workload

```python
# CPU-bound work: match core count
cluster.use(CPUWorker, count=os.cpu_count())

# IO-bound work: can have more instances
cluster.use(IOWorker, count=20)

# Light work: single instance is fine
cluster.use(ConfigService, count=1)
```

### 2. Use max_concurrent with Clusters

```python
class WorkerService(MessageService):
    def __init__(self, ctx):
        super().__init__(ctx, MessageServiceOptions(
            max_concurrent=5  # Each instance handles 5 at a time
        ))

# 4 instances × 5 concurrent = 20 total parallel messages
cluster.use(WorkerService, count=4)
```

### 3. Group Related Services

```python
# Run related services together
cluster.use(OrderService)
cluster.use(InventoryService)
cluster.use(PaymentService)

# Or run workers separately
worker_cluster = ServiceCluster(ctx)
worker_cluster.use(HeavyWorker, count=10)
```

---

See also: [MessageService](message-service.md) | [Context](context.md)
