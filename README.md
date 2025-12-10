# Protobus Python

A lightweight, scalable microservices message bus for Python. Leverages RabbitMQ for message routing and load balancing, combined with Protocol Buffers for efficient, type-safe serialization.

> **Note:** This is the official Python port of [Protobus](https://github.com/ArielLaub/protobus), originally written in TypeScript. The API and architecture are designed to be as close to the original as possible.

## Features

- **RPC Communication**: Request-response pattern over message queues
- **Event System**: Publish-subscribe with topic-based routing and wildcards
- **Auto-Reconnection**: Exponential backoff with jitter for resilient connections
- **Message Retry**: Automatic retry with dead-letter queue (DLQ) support
- **Custom Types**: Extensible type system (BigInt, Timestamp built-in)
- **Async/Await**: Built on asyncio and aio-pika for modern Python

## Requirements

- Python 3.10 or higher
- RabbitMQ 3.8 or higher

## Installation

```bash
pip install protobus
```

Or install from source:

```bash
git clone https://github.com/ArielLaub/protobus-py.git
cd protobus-py
pip install -e .
```

## Quick Start

### 1. Start RabbitMQ

```bash
docker-compose up -d
```

### 2. Create a Service

```python
from protobus import Context, MessageService

class CalculatorService(MessageService):
    @property
    def service_name(self) -> str:
        return "calculator.MathService"

    @property
    def proto_file_name(self) -> str:
        return "calculator.proto"

    async def add(self, data: dict, actor: str, correlation_id: str) -> dict:
        return {"result": data["a"] + data["b"]}

    async def multiply(self, data: dict, actor: str, correlation_id: str) -> dict:
        return {"result": data["a"] * data["b"]}
```

### 3. Run the Service

```python
import asyncio
from protobus import Context

async def main():
    # Initialize context
    ctx = Context()
    await ctx.init("amqp://guest:guest@localhost:5672/")

    # Create and start service
    service = CalculatorService(ctx)
    ctx.factory.parse("", service.service_name)
    await service.init()

    print("Calculator service running...")

    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        await ctx.close()

asyncio.run(main())
```

### 4. Create a Client

```python
import asyncio
from protobus import Context, ServiceProxy

async def main():
    ctx = Context()
    await ctx.init("amqp://guest:guest@localhost:5672/")

    # Create proxy for the calculator service
    calc = ServiceProxy(ctx, "calculator.MathService")
    await calc.init()

    # Make RPC calls
    result = await calc.add({"a": 5, "b": 3})
    print(f"5 + 3 = {result['result']}")  # Output: 5 + 3 = 8

    result = await calc.multiply({"a": 4, "b": 7})
    print(f"4 * 7 = {result['result']}")  # Output: 4 * 7 = 28

    await ctx.close()

asyncio.run(main())
```

## Documentation

- [Getting Started](docs/getting-started.md) - Installation and first service
- [Architecture](docs/architecture.md) - System design and components
- [Configuration](docs/configuration.md) - Environment variables and options
- [Message Flow](docs/message-flow.md) - How messages move through the system
- [Troubleshooting](docs/troubleshooting.md) - Common issues and solutions

### API Reference

- [Context](docs/api/context.md) - Connection management
- [MessageService](docs/api/message-service.md) - Building services
- [ServiceProxy](docs/api/service-proxy.md) - Calling services
- [ServiceCluster](docs/api/service-cluster.md) - Managing multiple services
- [Events](docs/api/events.md) - Publish-subscribe patterns

### Advanced Topics

- [Error Handling](docs/advanced/error-handling.md) - Retries and DLQ
- [Custom Logger](docs/advanced/custom-logger.md) - Logging integration
- [Custom Types](docs/advanced/custom-types.md) - Extending the type system

## Sample Application

The `sample/combatGame` directory contains a complete example - a turn-based battle royale game with 6 AI players demonstrating:

- Multiple services communicating via RPC
- Event-based game state synchronization
- Different player strategies

Run it:

```bash
python sample/combatGame/game_runner.py
```

## Original Project

This is the Python port of [Protobus](https://github.com/ArielLaub/protobus) (TypeScript). The original project provides identical functionality for Node.js/TypeScript environments.

## License

MIT License - Copyright (c) Remarkable Games Ltd.

See [LICENSE](LICENSE) for details.
