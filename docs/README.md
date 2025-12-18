# Protobus Python Documentation

Welcome to the Protobus Python documentation. This is the Python port of [Protobus](https://github.com/ArielLaub/protobus), a TypeScript microservices message bus.

## Overview

Protobus is a lightweight framework for building microservices that communicate over RabbitMQ using Protocol Buffers for serialization. It provides:

- **RPC Communication** - Request-response pattern over message queues
- **Event System** - Publish-subscribe with topic-based routing
- **Auto-Reconnection** - Resilient connections with exponential backoff
- **Message Retry** - Automatic retry with dead-letter queue support

## Getting Started

New to Protobus? Start here:

1. [Getting Started Guide](getting-started.md) - Installation and your first service
2. [Architecture Overview](architecture.md) - Understanding the system design
3. [Configuration](configuration.md) - Environment variables and options

## Core Concepts

- [Message Flow](message-flow.md) - How messages move through the system
- [Troubleshooting](troubleshooting.md) - Common issues and solutions
- [Known Issues](known-issues.md) - Current limitations

## CLI Tools

- [CLI Tools](cli.md) - Generate types and service stubs from .proto files

## API Reference

Detailed documentation for each component:

- [Context](api/context.md) - Connection management and initialization
- [MessageService](api/message-service.md) - Building RPC services
- [RunnableService](api/runnable-service.md) - Services with lifecycle management
- [ServiceProxy](api/service-proxy.md) - Calling remote services
- [ServiceCluster](api/service-cluster.md) - Managing multiple services
- [Events](api/events.md) - Publishing and subscribing to events

## Advanced Topics

- [Error Handling](advanced/error-handling.md) - Retries, DLQ, and HandledError
- [Custom Logger](advanced/custom-logger.md) - Integrating with your logging system
- [Custom Types](advanced/custom-types.md) - Extending the type system

## Sample Application

Check out the `sample/combatGame` directory for a complete example demonstrating:

- Multiple services communicating via RPC
- Event-based state synchronization
- Real-world usage patterns

## Original Project

This documentation mirrors the structure of the original [Protobus TypeScript documentation](https://github.com/ArielLaub/protobus/tree/master/docs). The Python port maintains API compatibility where possible.

## License

MIT License - Copyright (c) Remarkable Games Ltd.
