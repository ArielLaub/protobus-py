# Getting Started

This guide will help you build your first microservice with Protobus Python.

> **Note:** This is the Python port of [Protobus](https://github.com/ArielLaub/protobus). See the [original documentation](https://github.com/ArielLaub/protobus/blob/master/docs/getting-started.md) for the TypeScript version.

## Prerequisites

- Python 3.10 or higher
- RabbitMQ 3.8 or higher
- Basic understanding of async/await in Python

## Installation

### Install Protobus

```bash
pip install protobus
```

Or from source:

```bash
git clone https://github.com/ArielLaub/protobus-py.git
cd protobus-py
pip install -e .
```

### Start RabbitMQ

Using Docker:

```bash
docker run -d --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3-management
```

Or use the included docker-compose.yml:

```bash
docker-compose up -d
```

Access the management UI at http://localhost:15672 (guest/guest).

## Step 1: Define Your Service Schema

Create a Protocol Buffer file defining your service interface:

```protobuf
// calculator.proto
syntax = "proto3";

package calculator;

message AddRequest {
    int32 a = 1;
    int32 b = 2;
}

message AddResponse {
    int32 result = 1;
}

message CalculationEvent {
    string operation = 1;
    int32 a = 2;
    int32 b = 3;
    int32 result = 4;
}

service MathService {
    rpc add(AddRequest) returns (AddResponse);
}
```

## Step 2: Create a Context

The Context manages your connection to RabbitMQ and message serialization:

```python
from protobus import Context

async def create_context():
    ctx = Context()
    await ctx.init("amqp://guest:guest@localhost:5672/")
    return ctx
```

## Step 3: Implement Your Service

Extend `MessageService` to create your RPC handlers:

```python
from protobus import Context, MessageService

class CalculatorService(MessageService):
    @property
    def service_name(self) -> str:
        # Full service name = Package name + Service name
        return "calculator.MathService"

    @property
    def proto_file_name(self) -> str:
        return "calculator.proto"

    async def add(self, data: dict, actor: str, correlation_id: str) -> dict:
        """Handle add RPC requests."""
        a = data.get("a", 0)
        b = data.get("b", 0)
        result = a + b

        # Optionally publish an event
        await self.publish_event("CalculationPerformed", {
            "operation": "add",
            "a": a,
            "b": b,
            "result": result
        })

        return {"result": result}
```

## Step 4: Start the Service

Initialize and run your service:

```python
import asyncio
from protobus import Context

async def main():
    # Create context
    ctx = Context()
    await ctx.init("amqp://guest:guest@localhost:5672/")

    # Create service
    service = CalculatorService(ctx)
    ctx.factory.parse("", service.service_name)
    await service.init()

    print(f"Service {service.service_name} is running...")

    # Keep the service running
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        await ctx.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Step 5: Create a Client

Use `ServiceProxy` to call your service:

```python
import asyncio
from protobus import Context, ServiceProxy

async def main():
    # Create context
    ctx = Context()
    await ctx.init("amqp://guest:guest@localhost:5672/")

    # Create proxy for the calculator service
    calculator = ServiceProxy(ctx, "calculator.MathService")
    await calculator.init()

    # Make RPC calls
    result = await calculator.add({"a": 10, "b": 5})
    print(f"10 + 5 = {result['result']}")

    await ctx.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Step 6: Subscribe to Events

Services can subscribe to events published by other services:

```python
from protobus import MessageService

class MonitorService(MessageService):
    @property
    def service_name(self) -> str:
        return "monitor.LogService"

    @property
    def proto_file_name(self) -> str:
        return "monitor.proto"

    async def init(self):
        await super().init()

        # Subscribe to calculation events
        await self.subscribe_event(
            "CalculationPerformed",
            self.on_calculation
        )

    async def on_calculation(self, data: dict, topic: str):
        """Handle calculation events."""
        print(f"Calculation: {data['a']} {data['operation']} {data['b']} = {data['result']}")
```

## Advanced: Service Cluster

Run multiple services in a single process:

```python
from protobus import Context, ServiceCluster

async def main():
    ctx = Context()
    await ctx.init("amqp://guest:guest@localhost:5672/")

    # Create cluster
    cluster = ServiceCluster(ctx)

    # Register services (can specify instance count for load balancing)
    cluster.use(CalculatorService, count=2)  # 2 instances
    cluster.use(MonitorService, count=1)

    # Initialize all services
    await cluster.init()

    print(f"Running services: {cluster.service_names}")

    # Keep running...
```

## Type-Safe Proxy (Optional)

For better IDE support, create a typed wrapper:

```python
from typing import TypedDict
from protobus import Context, ServiceProxy

class AddRequest(TypedDict):
    a: int
    b: int

class AddResponse(TypedDict):
    result: int

class CalculatorProxy:
    def __init__(self, proxy: ServiceProxy):
        self._proxy = proxy

    async def add(self, request: AddRequest) -> AddResponse:
        return await self._proxy.add(request)

# Usage
proxy = ServiceProxy(ctx, "calculator.MathService")
await proxy.init()
calculator = CalculatorProxy(proxy)

result = await calculator.add({"a": 5, "b": 3})  # Type-checked!
```

## Next Steps

- [Architecture](architecture.md) - Understand the system design
- [Configuration](configuration.md) - Customize behavior with environment variables
- [Message Flow](message-flow.md) - Learn how messages flow through the system
- [Error Handling](advanced/error-handling.md) - Handle failures gracefully
