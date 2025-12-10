# Custom Types

Protobus includes a type registry system for handling special types that require custom serialization, such as BigInt, Timestamps, and domain-specific types.

## Built-in Custom Types

### BigInt

Handles large integers that exceed JavaScript's safe integer limit:

```python
from protobus import BigInt

# Create BigInt
big = BigInt(9007199254740993)  # Larger than JS MAX_SAFE_INTEGER

# Use in messages
await proxy.process_large_number({"value": big})
```

### Timestamp

Represents timestamps with millisecond precision:

```python
from protobus import Timestamp
import time

# Create timestamp
now = Timestamp(int(time.time() * 1000))

# Use in messages
await proxy.schedule_event({"time": now})
```

## How Custom Types Work

### Serialization Flow

```
Python Object → encode() → JSON-safe dict → JSON string → RabbitMQ
RabbitMQ → JSON string → JSON-safe dict → decode() → Python Object
```

### Type Marker Format

Custom types are serialized with a special marker:

```json
{
  "__customType__": "BigInt",
  "value": "9007199254740993"
}
```

## Creating Custom Types

### Step 1: Define the Type Class

```python
from protobus.custom_types import CustomType
from typing import Any

class Money(CustomType):
    """Custom type for monetary values with currency."""

    type_name = "Money"

    def __init__(self, amount: int, currency: str):
        self.amount = amount  # Amount in cents
        self.currency = currency

    def to_serializable(self) -> dict[str, Any]:
        """Convert to JSON-serializable format."""
        return {
            "amount": self.amount,
            "currency": self.currency
        }

    @classmethod
    def from_serializable(cls, data: dict[str, Any]) -> "Money":
        """Create instance from serialized data."""
        return cls(data["amount"], data["currency"])
```

### Step 2: Register the Type

```python
from protobus.custom_types import type_registry

# Register your custom type
type_registry.register(Money)
```

### Step 3: Use in Messages

```python
# Now Money objects are automatically serialized/deserialized
price = Money(9999, "USD")  # $99.99

await proxy.create_order({
    "item": "Widget",
    "price": price
})
```

## Type Registry API

### register()

```python
type_registry.register(type_class: Type[CustomType]) -> None
```

Register a custom type for serialization.

### encode()

```python
type_registry.encode(value: Any) -> Any
```

Encode a value, handling custom types recursively.

### decode()

```python
type_registry.decode(value: Any) -> Any
```

Decode a value, restoring custom types recursively.

### is_custom_type()

```python
type_registry.is_custom_type(value: Any) -> bool
```

Check if a value is a serialized custom type marker.

## Example: UUID Type

```python
import uuid
from protobus.custom_types import CustomType, type_registry

class UUID(CustomType):
    """Custom type for UUIDs."""

    type_name = "UUID"

    def __init__(self, value: uuid.UUID | str):
        if isinstance(value, str):
            self.value = uuid.UUID(value)
        else:
            self.value = value

    def to_serializable(self) -> dict:
        return {"value": str(self.value)}

    @classmethod
    def from_serializable(cls, data: dict) -> "UUID":
        return cls(data["value"])

    def __str__(self):
        return str(self.value)

    def __eq__(self, other):
        if isinstance(other, UUID):
            return self.value == other.value
        return False


# Register
type_registry.register(UUID)

# Usage
order_id = UUID(uuid.uuid4())
await proxy.get_order({"id": order_id})
```

## Example: Decimal Type

```python
from decimal import Decimal as PyDecimal
from protobus.custom_types import CustomType, type_registry

class Decimal(CustomType):
    """Custom type for precise decimal numbers."""

    type_name = "Decimal"

    def __init__(self, value: PyDecimal | str | float):
        if isinstance(value, PyDecimal):
            self.value = value
        else:
            self.value = PyDecimal(str(value))

    def to_serializable(self) -> dict:
        return {"value": str(self.value)}

    @classmethod
    def from_serializable(cls, data: dict) -> "Decimal":
        return cls(PyDecimal(data["value"]))

    def __float__(self):
        return float(self.value)

    def __str__(self):
        return str(self.value)


# Register
type_registry.register(Decimal)

# Usage - perfect for financial calculations
tax_rate = Decimal("0.0825")
await proxy.calculate_tax({"rate": tax_rate, "amount": 100})
```

## Example: DateTime Type

```python
from datetime import datetime
from protobus.custom_types import CustomType, type_registry

class DateTime(CustomType):
    """Custom type for datetime with timezone support."""

    type_name = "DateTime"

    def __init__(self, value: datetime | str):
        if isinstance(value, str):
            self.value = datetime.fromisoformat(value)
        else:
            self.value = value

    def to_serializable(self) -> dict:
        return {"value": self.value.isoformat()}

    @classmethod
    def from_serializable(cls, data: dict) -> "DateTime":
        return cls(data["value"])


# Register
type_registry.register(DateTime)

# Usage
scheduled = DateTime(datetime.now())
await proxy.schedule_task({"run_at": scheduled})
```

## Nested Custom Types

Custom types can be nested within other data structures:

```python
# Types are encoded/decoded recursively
data = {
    "order_id": UUID(uuid.uuid4()),
    "items": [
        {"name": "Widget", "price": Money(999, "USD")},
        {"name": "Gadget", "price": Money(1999, "USD")}
    ],
    "created_at": DateTime(datetime.now()),
    "total": Money(2998, "USD")
}

# All custom types are automatically handled
await proxy.process_order(data)
```

## Best Practices

### 1. Use Descriptive Type Names

```python
# Good - clear and unique
class OrderId(CustomType):
    type_name = "OrderId"

# Bad - too generic, may conflict
class Id(CustomType):
    type_name = "Id"
```

### 2. Make Types Immutable

```python
class Point(CustomType):
    type_name = "Point"

    def __init__(self, x: float, y: float):
        self._x = x
        self._y = y

    @property
    def x(self) -> float:
        return self._x

    @property
    def y(self) -> float:
        return self._y

    # No setters - immutable
```

### 3. Implement Equality

```python
class Money(CustomType):
    def __eq__(self, other):
        if isinstance(other, Money):
            return self.amount == other.amount and self.currency == other.currency
        return False

    def __hash__(self):
        return hash((self.amount, self.currency))
```

### 4. Add String Representation

```python
class Money(CustomType):
    def __str__(self):
        dollars = self.amount / 100
        return f"${dollars:.2f} {self.currency}"

    def __repr__(self):
        return f"Money({self.amount}, '{self.currency}')"
```

### 5. Register Types at Startup

```python
# types.py
from protobus.custom_types import type_registry

def register_custom_types():
    """Register all application custom types."""
    type_registry.register(Money)
    type_registry.register(UUID)
    type_registry.register(DateTime)
    type_registry.register(Decimal)

# main.py
from types import register_custom_types

async def main():
    # Register types before using protobus
    register_custom_types()

    ctx = Context()
    await ctx.init("amqp://localhost")
    # ...
```

## Compatibility Notes

### Cross-Language Support

Custom types use a JSON-based wire format, making them compatible with other language implementations (like the original TypeScript protobus):

```json
{
  "__customType__": "Money",
  "value": {
    "amount": 999,
    "currency": "USD"
  }
}
```

### Protocol Buffers Alternative

For more efficient serialization, consider using Protocol Buffers:

```protobuf
// money.proto
message Money {
  int64 amount = 1;
  string currency = 2;
}
```

The Python protobus library supports Protocol Buffers through the `proto_file_name` property on services, though the default JSON encoding works well for most use cases.

---

See also: [MessageService](../api/message-service.md) | [Architecture](../architecture.md)
