# Schema Design

> The `.proto` file is the contract. Everything else — the queue name, the routing key, the generated types — follows from it.

**Read this if** you are about to write or change a schema, or you want to know which changes are safe to deploy.

| | |
|---|---|
| **Prerequisites** | [Getting Started](./getting-started.md) |
| **Next** | [CLI](../reference/cli.md) — generating types from it · [Custom Types](../reference/custom-types.md) |
| **Source** | [`protobus/proto_parser.py`](../../protobus/proto_parser.py) · [`protobus/message_factory.py`](../../protobus/message_factory.py) · [`protobus/custom_types.py`](../../protobus/custom_types.py) |

## Basic Structure

```protobuf
syntax = "proto3";
package MyPackage;

// Messages
message MyRequest { ... }
message MyResponse { ... }

// Events
message MyEvent { ... }

// Service
service MyService {
    rpc myMethod(MyPackage.MyRequest) returns(MyPackage.MyResponse);
}
```

## Naming Conventions

### Package Names
- Use PascalCase: `OrderManagement`, `UserAuth`
- Keep concise but descriptive
- Represents a bounded context or domain

### Service Names
- Use PascalCase: `OrderService`, `PaymentProcessor`
- Full name is `Package.ServiceName`

### Message Names
- Use PascalCase: `CreateOrderRequest`, `OrderCreatedEvent`
- Suffix requests with `Request`
- Suffix responses with `Response`
- Suffix events with `Event`

### Field Names
- Use snake_case: `order_id`, `created_at`
- Be descriptive: `user_email` not `email`

## Message Design

### Request Messages

```protobuf
message CreateOrderRequest {
    // Required fields first
    string user_id = 1;
    repeated OrderItem items = 2;

    // Optional fields
    string coupon_code = 3;
    ShippingAddress shipping_address = 4;

    // Metadata
    string idempotency_key = 10;
}
```

### Response Messages

```protobuf
message CreateOrderResponse {
    // Primary result
    string order_id = 1;

    // Additional info
    OrderStatus status = 2;
    int64 estimated_delivery = 3;

    // Computed values
    Money total_amount = 4;
}
```

### Event Messages

```protobuf
message OrderCreatedEvent {
    // Event identification
    string event_id = 1;
    int64 timestamp = 2;

    // Entity reference
    string order_id = 3;

    // Context
    string user_id = 4;
    string source = 5;  // What triggered this

    // Relevant data (not full entity)
    Money total_amount = 6;
    int32 item_count = 7;
}
```

## Field Types

### Scalar Types

| Proto Type | Python | Use For |
|------------|--------|---------|
| `string` | `str` | Text, IDs, UUIDs |
| `int32` | `int` | Small integers |
| `int64` | `int` | Timestamps, large integers |
| `bool` | `bool` | Flags |
| `bytes` | `bytes` | Binary data |
| `double` | `float` | Floating point |
| `bigint` | `int` | Large integers (uint256, etc.) |
| `timestamp` | `datetime` | Instants, millisecond precision |

#### 64-bit integers decode to `int`

`int64`, `uint64`, `sint64`, `fixed64` and `sfixed64` decode to a Python `int`,
which is exact across the whole range. (protobus-py 1.x returned decimal
strings, and the TypeScript port still does, because a JavaScript number cannot
represent anything past 2<sup>53</sup> exactly. The bytes on the wire are the
same; each peer decodes into whatever its language holds natively.)

Encoding is permissive: an `int`, a whole-valued `float`, or a decimal or hex
string are all accepted; anything else raises `TypeError` naming the field.

```python
# A field declared `int64 recorded_at = 1;`, once decoded.
from datetime import datetime, timezone

reading = {"recorded_at": 1705314600000}
when = datetime.fromtimestamp(reading["recorded_at"] / 1000, tz=timezone.utc)
```

For values that genuinely exceed 64 bits, use protobus's own `bigint` type
below instead.

#### What a decoded message looks like

Decoding follows the same rules as the TypeScript port's `toObject()`, so a
message decoded by either port reads the same:

| Field | Unset on the wire decodes as |
|---|---|
| scalar (`string`, `int32`, `bool`, …) | its zero value — `""`, `0`, `False` |
| `enum` | the zero value's **name**, as a `str` |
| message | `None` |
| `repeated` | `[]` |
| `map<…>` | `{}` |
| `optional` scalar, or a `oneof` member | **absent** from the dict |

Proto3 does not distinguish "unset" from "zero" for a plain scalar, so
`request.get("count")` is `0` either way. Declare a field `optional` when the
distinction matters — see [Migration → The dangerous one](../migration.md#the-dangerous-one-proto3-zero-values)
for what this changed from 1.x.

### Built-in Custom Types

Protobus provides built-in custom types that extend the standard protobuf scalar types:

| Type | Python | Description |
|------|--------|-------------|
| `bigint` | `int` | Large integers (uint256 compatible, 32 bytes) |
| `timestamp` | `datetime` | Timestamps (milliseconds, stored as int64) |

Both are registered at import time and use the same wire format as the
TypeScript port, so a `bigint` written by one is read by the other.

#### BigInt Type (Web3/Crypto)

The `bigint` type handles large integers commonly used in Web3 applications:

- Serializes to 32 bytes (big-endian, uint256 compatible)
- Deserializes to a Python `int`
- Accepts an `int` or a `str` (decimal or hex) as input; a negative value or
  one above 2<sup>256</sup>−1 raises `ValueError`

```protobuf
message TokenTransfer {
    string from = 1;
    string to = 2;
    bigint amount = 3;      // Native bigint support
    bigint gas_price = 4;
}
```

```python
# Using a Python int
await token_service.transfer({
    "amount": 1_000_000_000_000_000_000,  # 1 ETH in wei
})

# Using hex or decimal strings
await token_service.transfer({
    "amount": "0xde0b6b3a7640000",  # hex
})

# Response always returns an int
balance = await token_service.getBalance({})
print(type(balance["value"]))  # <class 'int'>
```

#### Timestamp Type

The `timestamp` type provides convenient `datetime` handling:

- Serializes to int64 (milliseconds since epoch)
- Deserializes to a **timezone-aware UTC** `datetime`
- Accepts a `datetime` (aware, or naive — read in the process's local zone),
  an ISO-8601 string, or a number of milliseconds as input

```protobuf
message Event {
    string name = 1;
    timestamp created_at = 2;
    timestamp updated_at = 3;
}
```

```python
from datetime import datetime, timezone

# Using datetime objects
await event_service.create({
    "name": "signup",
    "created_at": datetime.now(timezone.utc),
})

# Using ISO strings
await event_service.create({
    "name": "signup",
    "created_at": "2024-01-15T10:30:00.000Z",
})

# Response returns datetime objects
event = await event_service.get({})
print(event["created_at"].tzinfo)  # UTC
```

### Custom Type Registration

You can define your own custom types with a `CustomType`:

```python
import asyncio
import uuid

from protobus import Context, CustomType

UuidType = CustomType(
    name="uuid",            # how it is written in a .proto
    wire_type="bytes",      # how it travels
    py_type="str",          # what generated typing calls it
    encode=lambda value: uuid.UUID(value).bytes,
    decode=lambda data: str(uuid.UUID(bytes=bytes(data))),
)


async def main() -> None:
    context = Context()

    # Register BEFORE init(): init() parses your .proto files, and a schema
    # using `uuid` cannot be parsed until the type exists.
    context.factory.register_type(UuidType)

    await context.init("amqp://localhost", ["./proto"])


asyncio.run(main())
```

The schema that uses it **must declare `syntax = "proto3";`**:

```protobuf
syntax = "proto3";
package Entities;

message Entity {
    uuid id = 1;
    string name = 2;
}
```

> [!WARNING]
> Without the syntax line the file is read as proto2. protobus-py's parser
> tolerates the missing field labels, but the TypeScript port's does not — it
> reports the custom type as `illegal token 'uuid'` — so a schema shared across
> the two ports must carry the line. A type that is not registered fails with
> `ProtoParseError: unknown type 'uuid' (… not a registered custom type:
> bigint, timestamp)`, naming the file and line.

> [!CAUTION]
> **Custom type names are process-wide.** `register_type()` adds the type to
> that factory's root, but the codec itself goes into a module-level registry
> shared by everything in the process. Two factories cannot hold different
> definitions of the same name, and a name registered through one is visible to
> all of them. Re-registering a name is allowed and refreshes its codec —
> unless the new definition disagrees about `wire_type`, which is refused with
> `CustomTypeConflictError`, because the wrapper message was fixed at first
> registration. Namespace your names if a process hosts more than one schema.
>
> Full account: [Custom Types](../reference/custom-types.md).

Available wire types: `bytes`, `int64`, `uint64`, `string`, `int32`, `uint32`, `double`

### Timestamps

```protobuf
// Option 1: the built-in custom type (recommended): decodes to an aware datetime
timestamp created_at = 1;

// Option 2: Unix timestamp
int64 created_at = 1;  // milliseconds since epoch

// Option 3: ISO string
string created_at = 1;  // "2024-01-15T10:30:00Z"
```

### Money

```protobuf
message Money {
    int64 amount = 1;      // In smallest unit (cents)
    string currency = 2;   // ISO 4217: "USD", "EUR"
}

// Usage
message Order {
    Money total = 1;
    Money tax = 2;
}
```

### Enums

```protobuf
enum OrderStatus {
    ORDER_STATUS_UNKNOWN = 0;  // Always have unknown/default
    ORDER_STATUS_PENDING = 1;
    ORDER_STATUS_PROCESSING = 2;
    ORDER_STATUS_SHIPPED = 3;
    ORDER_STATUS_DELIVERED = 4;
    ORDER_STATUS_CANCELLED = 5;
}
```

### Repeated Fields (Arrays)

```protobuf
message Order {
    repeated OrderItem items = 1;
    repeated string tags = 2;
}
```

### Nested Messages

```protobuf
message Order {
    message Item {
        string product_id = 1;
        int32 quantity = 2;
        Money price = 3;
    }

    repeated Item items = 1;
}
```

## Service Design

### One Operation Per Method

```protobuf
// Good: Single responsibility
service OrderService {
    rpc CreateOrder(CreateOrderRequest) returns(CreateOrderResponse);
    rpc GetOrder(GetOrderRequest) returns(GetOrderResponse);
    rpc UpdateOrder(UpdateOrderRequest) returns(UpdateOrderResponse);
    rpc CancelOrder(CancelOrderRequest) returns(CancelOrderResponse);
}

// Avoid: Multiple operations in one method
service OrderService {
    rpc ManageOrder(ManageOrderRequest) returns(ManageOrderResponse);
    // Where ManageOrderRequest has operation_type enum
}
```

### Request/Response Per Method

```protobuf
// Good: Dedicated types
rpc CreateOrder(CreateOrderRequest) returns(CreateOrderResponse);
rpc GetOrder(GetOrderRequest) returns(GetOrderResponse);

// Avoid: Reusing types
rpc CreateOrder(OrderRequest) returns(OrderResponse);
rpc UpdateOrder(OrderRequest) returns(OrderResponse);
```

## Evolving Schemas

### Adding Fields

```protobuf
// v1
message Order {
    string order_id = 1;
    string user_id = 2;
}

// v2 - Safe to add new fields
message Order {
    string order_id = 1;
    string user_id = 2;
    string notes = 3;        // New field - backwards compatible
    Money discount = 4;      // New field - backwards compatible
}
```

### Field Number Rules

- Never reuse field numbers
- Reserved removed fields

```protobuf
message Order {
    reserved 3, 4;              // Removed fields
    reserved "old_field";       // Removed field names

    string order_id = 1;
    string user_id = 2;
    // field 3 was 'status' (removed)
    // field 4 was 'priority' (removed)
    string notes = 5;
}
```

### Breaking Changes (Avoid)

- Changing field types
- Changing field numbers
- Removing fields a peer still reads
- Renaming messages used in services

## Complete Example

The files under the locations passed to `Context.init()` are loaded in
dependency order whatever order they are found in: a file that refers to a type
nothing has declared yet is set aside until something declares it. The
dependency is derived from the types a file actually uses, not from its
`import` lines, so a type whose file was never loaded is reported as
`ProtoParseError: unknown type 'common.Money'` rather than as a missing file.

```protobuf
syntax = "proto3";
package Orders;

import "common/money.proto";

// Enums
enum OrderStatus {
    ORDER_STATUS_UNKNOWN = 0;
    ORDER_STATUS_PENDING = 1;
    ORDER_STATUS_CONFIRMED = 2;
    ORDER_STATUS_SHIPPED = 3;
    ORDER_STATUS_DELIVERED = 4;
    ORDER_STATUS_CANCELLED = 5;
}

// Common messages
message Address {
    string street = 1;
    string city = 2;
    string state = 3;
    string postal_code = 4;
    string country = 5;
}

message OrderItem {
    string product_id = 1;
    string product_name = 2;
    int32 quantity = 3;
    common.Money unit_price = 4;
}

// Request/Response messages
message CreateOrderRequest {
    string user_id = 1;
    repeated OrderItem items = 2;
    Address shipping_address = 3;
    string coupon_code = 4;
    string idempotency_key = 10;
}

message CreateOrderResponse {
    string order_id = 1;
    OrderStatus status = 2;
    common.Money total = 3;
}

message GetOrderRequest {
    string order_id = 1;
}

message GetOrderResponse {
    string order_id = 1;
    string user_id = 2;
    repeated OrderItem items = 3;
    Address shipping_address = 4;
    OrderStatus status = 5;
    common.Money subtotal = 6;
    common.Money tax = 7;
    common.Money total = 8;
    int64 created_at = 9;
    int64 updated_at = 10;
}

message CancelOrderRequest {
    string order_id = 1;
    string reason = 2;
}

message CancelOrderResponse {
    bool success = 1;
    string message = 2;
}

// Event messages
message OrderCreatedEvent {
    string event_id = 1;
    int64 timestamp = 2;
    string order_id = 3;
    string user_id = 4;
    common.Money total = 5;
    int32 item_count = 6;
}

message OrderShippedEvent {
    string event_id = 1;
    int64 timestamp = 2;
    string order_id = 3;
    string tracking_number = 4;
    string carrier = 5;
}

message OrderCancelledEvent {
    string event_id = 1;
    int64 timestamp = 2;
    string order_id = 3;
    string reason = 4;
    string cancelled_by = 5;
}

// Service definition
service OrderService {
    rpc CreateOrder(Orders.CreateOrderRequest) returns(Orders.CreateOrderResponse);
    rpc GetOrder(Orders.GetOrderRequest) returns(Orders.GetOrderResponse);
    rpc CancelOrder(Orders.CancelOrderRequest) returns(Orders.CancelOrderResponse);
}
```

---

<div align="center">

**[← Getting Started](./getting-started.md)** · **[Docs index](../README.md)** · **[Events →](./events.md)**

</div>
