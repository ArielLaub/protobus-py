# Message Flow

This document explains how messages flow through Protobus, including the encoding/decoding process.

> **Note:** This is the Python port of [Protobus](https://github.com/ArielLaub/protobus). See the [original message flow docs](https://github.com/ArielLaub/protobus/blob/master/docs/message-flow.md) for the TypeScript version.

## Message Encoding

Protobus uses a container-based encoding scheme:

```
┌─────────────────────────────────────────┐
│         Container (Outer Layer)         │
│  ┌───────────────────────────────────┐  │
│  │ • method/type (string)            │  │
│  │ • actor/topic (string)            │  │
│  │ • data ────────────────────────┐  │  │
│  │   ┌────────────────────────┐   │  │  │
│  │   │ Actual Message (dict)  │◄──┘  │  │
│  │   │ (Inner Layer)          │      │  │
│  │   └────────────────────────┘      │  │
│  └───────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

**Why containers?**
1. Outer layer provides routing metadata without knowing the message structure
2. Inner layer contains the actual business data
3. Services can route messages without understanding every message type
4. Enables generic middleware and logging

## RPC Request/Response Flow

### Complete Flow Diagram

```
    CLIENT                           BROKER                          SERVICE
      │                                │                                │
   1. │ build_request()                │                                │
      │  - Create RequestContainer     │                                │
      │  - Encode to JSON              │                                │
      │                                │                                │
   2. │ publish_message()              │                                │
      │ ──────────────────────────────>│                                │
      │  routing_key: REQUEST.svc.method                                │
      │  reply_to: callback_queue      │                                │
      │  correlation_id: uuid          │                                │
      │                                │                                │
   3. │                                │ Route via topic exchange       │
      │                                │ ──────────────────────────────>│
      │                                │                                │
   4. │                                │                                │ decode_request()
      │                                │                                │  - Parse JSON
      │                                │                                │  - Extract method
      │                                │                                │
   5. │                                │                                │ handler(data, actor, id)
      │                                │                                │  - Execute business logic
      │                                │                                │
   6. │                                │                                │ build_response()
      │                                │                                │  - Create ResponseContainer
      │                                │                                │  - Encode result/error
      │                                │                                │
   7. │                                │<────────────────────────────── │
      │                                │  routing_key: callback_queue   │
      │                                │  correlation_id: uuid          │
      │                                │                                │
   8. │<────────────────────────────── │                                │
      │  Match correlation_id          │                                │
      │  Resolve pending future        │                                │
      │                                │                                │
   9. │ decode_response()              │                                │
      │  - Parse JSON                  │                                │
      │  - Return result or raise error│                                │
```

### Request Encoding Details

```python
# What happens inside build_request()

# 1. Create the request envelope
envelope = {
    "method": "calculator.MathService.add",
    "data": {"a": 5, "b": 3},  # Actual request data
    "actor": "client-1"        # Optional caller ID
}

# 2. Encode to JSON bytes
request_bytes = json.dumps(envelope).encode("utf-8")

# 3. Publish with AMQP properties
message = Message(
    body=request_bytes,
    correlation_id="unique-uuid",
    reply_to="callback-queue-name"
)
```

### Response Encoding Details

```python
# Success response
envelope = {
    "method": "calculator.MathService.add",
    "result": {"data": {"result": 8}}
}

# Error response
envelope = {
    "method": "calculator.MathService.add",
    "error": {
        "message": "Division by zero",
        "code": "DIVISION_ERROR"
    }
}
```

## Event Flow

### Event Publishing

```
  PUBLISHER                         BROKER                      SUBSCRIBER(S)
      │                               │                               │
   1. │ publish_event()               │                               │
      │  type: "OrderCreated"         │                               │
      │  data: {order_id: 123}        │                               │
      │                               │                               │
   2. │ build_event()                 │                               │
      │  - Create EventContainer      │                               │
      │  - Encode to JSON             │                               │
      │                               │                               │
   3. │ ─────────────────────────────>│                               │
      │  routing_key: EVENT.OrderCreated                              │
      │                               │                               │
   4. │                               │ Route by topic pattern        │
      │                               │ ─────────────────────────────>│ Handler A (EVENT.Order*)
      │                               │ ─────────────────────────────>│ Handler B (EVENT.#)
      │                               │                               │
   5. │                               │                               │ decode_event()
      │                               │                               │ handler(data, topic)
```

### Wildcard Event Routing

The Trie data structure enables efficient wildcard matching:

```
Registered patterns:
  - EVENT.ORDER.*.CREATED     → Handler A
  - EVENT.ORDER.#             → Handler B
  - EVENT.ORDER.US.*.SHIPPED  → Handler C

Incoming event: EVENT.ORDER.US.123.CREATED
  ├─ Matches: EVENT.ORDER.*.CREATED    → Handler A ✓
  ├─ Matches: EVENT.ORDER.#            → Handler B ✓
  └─ No match: EVENT.ORDER.US.*.SHIPPED

Incoming event: EVENT.ORDER.EU.456.SHIPPED
  ├─ No match: EVENT.ORDER.*.CREATED
  ├─ Matches: EVENT.ORDER.#            → Handler B ✓
  └─ No match: EVENT.ORDER.US.*.SHIPPED
```

**Wildcard rules:**
- `*` matches exactly one word
- `#` matches zero or more words
- Words are separated by `.`

## Message Lifecycle

### Acknowledgment Flow

```
                    ┌─────────────┐
                    │   Message   │
                    │  Received   │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │   Process   │
                    │   Handler   │
                    └──────┬──────┘
                           │
              ┌────────────┴────────────┐
              │                         │
              ▼                         ▼
       ┌─────────────┐          ┌─────────────┐
       │   Success   │          │    Error    │
       └──────┬──────┘          └──────┬──────┘
              │                        │
              ▼                        │
       ┌─────────────┐                 │
       │     ACK     │                 │
       └─────────────┘                 │
                           ┌───────────┴───────────┐
                           │                       │
                           ▼                       ▼
                    ┌─────────────┐         ┌─────────────┐
                    │   Handled   │         │  Retriable  │
                    │    Error    │         │    Error    │
                    └──────┬──────┘         └──────┬──────┘
                           │                       │
                           ▼                       ▼
                    ┌─────────────┐         ┌─────────────┐
                    │     ACK     │         │ Retry/DLQ   │
                    │  (no retry) │         └─────────────┘
                    └─────────────┘
```

### Retry Flow

```
Message fails
     │
     ▼
┌─────────────────┐
│ Check retry     │
│ count < max?    │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
   Yes        No
    │         │
    ▼         ▼
┌────────┐  ┌────────┐
│ Retry  │  │  DLQ   │
│ Queue  │  │        │
└───┬────┘  └────────┘
    │
    │ (after TTL)
    ▼
┌────────────┐
│ Re-deliver │
│ to service │
└────────────┘
```

### Timeout Handling

```
     ┌─────────────────────┐
     │   Send Request      │
     │   Start Timer       │
     └──────────┬──────────┘
                │
                ▼
     ┌─────────────────────┐
     │   Waiting for       │
     │   Response...       │
     └──────────┬──────────┘
                │
       ┌────────┴────────┐
       │                 │
       ▼                 ▼
┌─────────────┐   ┌─────────────┐
│  Response   │   │   Timeout   │
│  Received   │   │  (600s def) │
└──────┬──────┘   └──────┬──────┘
       │                 │
       ▼                 ▼
┌─────────────┐   ┌─────────────┐
│   Resolve   │   │    Raise    │
│   Future    │   │ TimeoutError│
└─────────────┘   └─────────────┘
```

## Correlation ID Tracking

Every RPC call is tracked by a unique correlation ID:

```
Client                          Broker                          Service
  │                               │                               │
  │  correlation_id: "abc-123"    │                               │
  │ ─────────────────────────────>│                               │
  │                               │ correlation_id: "abc-123"     │
  │                               │ ─────────────────────────────>│
  │                               │                               │
  │                               │<───────────────────────────── │
  │                               │ correlation_id: "abc-123"     │
  │<───────────────────────────── │                               │
  │                               │                               │
  │  Match "abc-123" to           │                               │
  │  pending future → resolve     │                               │
```

## Performance Considerations

### Message Size

- JSON encoding is readable but larger than binary formats
- Container overhead is minimal (~100-200 bytes)
- Consider compression for very large payloads

### Latency Sources

1. Network round-trip to broker
2. JSON encoding/decoding
3. Queue processing time
4. Handler execution time

### Throughput Tips

- Use `max_concurrent` to limit parallel processing
- Run multiple service instances for horizontal scaling
- Consider message batching for high-volume events
- Monitor RabbitMQ queue depths

---

Next: [API Reference](api/context.md) | [Troubleshooting](troubleshooting.md)
