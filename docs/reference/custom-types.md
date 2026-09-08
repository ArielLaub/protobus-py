# Custom Types

> Teaching protobuf a scalar it does not have — `bigint`, `timestamp`, or one of your own — and the three rules that make it actually work.

**Read this if** you want a domain type to appear in a `.proto` as though it were built in, or you are looking at `unknown type 'money'` and cannot see what is wrong with your schema.

| | |
|---|---|
| **Prerequisites** | [Schema Design](../guide/schema.md) — you have written a `.proto` |
| **Next** | [Context](./api/context.md) · [Configuration](./configuration.md) |
| **Source** | [`protobus/custom_types.py`](../../protobus/custom_types.py) · [`protobus/message_factory.py`](../../protobus/message_factory.py) · [`tests/unit/test_custom_types.py`](../../tests/unit/test_custom_types.py) |

**On this page** — [What a custom type is](#what-a-custom-type-is) · [The API](#the-api) · [Worked example](#worked-example-money) · [When to register](#when-to-register) · [Registration is global](#registration-is-global) · [The built-ins](#the-built-ins) · [`CustomType` reference](#customtype-reference) · [Cross-language](#cross-language)

---

## What a custom type is

Protobuf has no `decimal`, no `datetime`, no 256-bit integer. The usual workaround is a wrapper message plus conversion code at both ends, repeated per field.

A custom type moves that conversion into the codec. You register a name, a wire representation and a pair of functions, and from then on the name is usable in a `.proto` exactly where a scalar would go:

```protobuf
syntax = "proto3";
package Billing;

message Invoice {
    string id    = 1;
    money  total = 2;
}

service Api {
    rpc issue (Invoice) returns (Invoice);
}
```

`money` is not a protobuf type. On the wire that field is a one-field wrapper message carrying a plain `string`; in your handler it is whatever your `decode` returns. Nothing else in the message changes, and a peer that has not registered `money` still reads the field as a message with one string field.

Under the hood a registration generates a message descriptor `message money { string value = 1; }` in a synthetic file, and the encoder and decoder convert at that boundary ([`protobus/custom_types.py`](../../protobus/custom_types.py), `wrapper_descriptor`). This is byte-for-byte the shape the TypeScript port produces, which is what makes `bigint` and `timestamp` interoperate.

---

## The API

> [!IMPORTANT]
> The working API is **`context.factory.register_type(custom_type)`** — an instance method on `MessageFactory`, reached through `context.factory`.
>
> The module-level `register_custom_type()` is also exported, but it only fills the process-wide registry; it does not add the type to any factory's root, so a schema parsed by that factory still cannot see it. Use `register_type`.

`register_type` returns the generated wrapper descriptor. You can ignore the return value; nothing in normal use needs it.

An unregistered name fails at parse time with the file and line:

```
ProtoParseError: unknown type 'money' (not a scalar, not declared in this file
or an imported one, and not a registered custom type: bigint, timestamp)
(billing.proto, line 6)
```

---

## Worked example: `Money`

A currency amount, carried as a `"USD:1999"` string on the wire and as a dataclass in application code. The schema is the block above.

```python
from dataclasses import dataclass

from protobus import Context, CustomType


@dataclass
class Money:
    currency: str
    cents: int


def _decode(data: str) -> Money:
    currency, cents = str(data).split(":")
    return Money(currency, int(cents))


MoneyType = CustomType(
    name="money",             # the token that appears in the .proto
    wire_type="string",       # how it is actually encoded
    py_type="Money",          # what `protobus generate` writes into the typing
    encode=lambda value: f"{value.currency}:{value.cents}",
    decode=_decode,
)


async def start() -> Context:
    context = Context()
    context.factory.register_type(MoneyType)          # BEFORE init, see below
    await context.init("amqp://guest:guest@localhost:5672/", ["./proto"])
    return context
```

A handler then receives and returns `Money` objects with no conversion code:

```python
from protobus import RunnableService


class BillingApi(RunnableService):
    service_name = "Billing.Api"

    async def issue(self, request: dict, actor: str, correlation_id: str) -> dict:
        total = request["total"] or Money("USD", 0)
        return {"id": request["id"], "total": Money(total.currency, total.cents + 50)}
```

`py_type` is a **string that is emitted verbatim** into generated typing — `total: Money`. It is not checked against anything, and the generator does not import `Money` for you. Point it at a type your generated module can see, or you get typing that does not import.

> [!TIP]
> `encode` is called with whatever the application passed, which will not always be your type: a JSON body, a value round-tripped through a queue, a test fixture. `BigIntType.encode` accepts an `int`, a decimal string, a hex string and a whole-valued float for exactly this reason. Be similarly tolerant, and fail loudly on input you cannot represent rather than coercing it.
>
> A value that is *already* in wire form — a `dict` `{"value": ...}` — is accepted as-is, so a message decoded by one process can be re-encoded by another without conversion.

---

## When to register

Registration must happen **before the schema that uses the type is parsed**. That is the whole rule; `init()` is not the boundary people assume.

`MessageFactory.init()` builds a fresh root, adds the built-ins, then re-adds everything registered so far, and only then loads the proto files ([`protobus/message_factory.py`](../../protobus/message_factory.py), `init`). A schema on disk that names an unregistered type fails right there.

| Order | Result |
|---|---|
| `register_type()`, then `factory.init([])`, then `factory.parse(schema)` | works |
| `factory.init([])`, then `register_type()`, then `factory.parse(schema)` | works |
| `factory.init([proto_dir])` where a file in `proto_dir` uses the type, then `register_type()` | **fails**: `ProtoParseError: unknown type 'money' …` |

Both working orders are pinned by tests — `test_allows_registering_custom_types_before_init` and `test_supports_registering_types_after_init` in [`tests/unit/test_custom_types.py`](../../tests/unit/test_custom_types.py).

Since `Context.init()` calls `factory.init(proto_locations)` as its first statement ([`protobus/context.py`](../../protobus/context.py)), the practical rule for an application is simple:

> [!IMPORTANT]
> Register on `context.factory` **before** `await context.init(...)`. `context.factory` exists from the moment the `Context` is constructed, so there is no reason to leave it later.

A service that supplies its own schema through `proto_file_name` rather than a proto directory has more room — that schema is parsed during `service.init()` — but the rule above is correct in both cases and costs nothing.

---

## Registration is global

> [!CAUTION]
> **A custom type is process-wide, not per factory.** `register_type` writes a module-level registry that every `MessageFactory` in the process reads at encode and decode time. Names are therefore global: the last registration of a name wins, and every factory sees it. Two factories cannot hold different definitions of `money`. Namespace your names — `acme_money`, not `money` — if the process hosts more than one schema, or if you publish a library that registers types.

Only the addition to a factory's root is per instance — which is why two factories do not otherwise share state (`test_two_factories_do_not_share_state`).

> [!NOTE]
> **Registering the same name twice is allowed** and refreshes its codec, so the last definition of a name wins. Re-registering a built-in "to be safe" — `factory.register_type(BigIntType)` — does what it looks like it does.
>
> One re-registration is refused, with `CustomTypeConflictError`: one that changes `wire_type`. The wrapper message is fixed at first registration, so accepting it would go on encoding in the original wire format while the caller believed it had changed.

---

## The built-ins

`bigint` and `timestamp` are registered at import time, before any factory exists ([`protobus/custom_types.py`](../../protobus/custom_types.py), bottom). They are available in every schema with no setup.

### `bigint`

| | |
|---|---|
| Wire type | `bytes` — **up to 32 bytes, big-endian, unsigned** (uint256-compatible); encoded at the full 32 bytes |
| Decodes to | `int` |
| Accepts | `int`, decimal string, `0x` hex string, whole-valued `float`; `None` encodes as 0 |
| Range | `0` … `2^256 - 1` (`BIGINT_MAX`) |

Out-of-range values raise a `ValueError` rather than being coerced. `-5` is **not** encoded as `5` and `2^256 + 7` is **not** truncated to `7`; both raise (`test_rejects_negative_values_instead_of_dropping_the_sign`, `test_rejects_values_that_do_not_fit_in_256_bits`). For money and on-chain amounts, failing loudly is the only safe behaviour. A `bool` is refused too, since `True` silently becoming `1` is the same class of mistake.

Decoding is bounded too: a wire value longer than 32 bytes raises instead of being decoded, so a malformed value cannot occupy the event loop (`test_cannot_be_made_to_spend_unbounded_cpu_on_one_value`).

An unset `bigint` field decodes as `None`, like any unset message field, not as `0`.

`bigint_to_bytes(value)` and `bytes_to_bigint(data)` are exported for use outside a message. They are what `BigIntType.encode` / `.decode` call and carry the same range checks:

```python
from protobus import bigint_to_bytes, bytes_to_bigint

wire = bigint_to_bytes("0xdeadbeef")      # 32 bytes, big-endian
print(len(wire))                          # 32
print(bytes_to_bigint(wire))              # 3735928559
print(bytes_to_bigint(b""))               # 0 — empty decodes to zero
```

### `timestamp`

| | |
|---|---|
| Wire type | `int64` — milliseconds since the Unix epoch |
| Decodes to | a **timezone-aware UTC** `datetime` |
| Accepts | `datetime`, `int`/`float` (ms), ISO-8601 string (a trailing `Z` is understood) |

A naive `datetime` is read in the process's local timezone, which is what `datetime.timestamp()` does — pass aware datetimes when the process timezone is not the one you mean (`test_a_naive_datetime_is_read_in_local_time`). Decoding to UTC rather than local time means the value round-trips identically on every machine and compares equal to what the TypeScript port's `Date` represents: an absolute instant.

> [!NOTE]
> `timestamp` is protobus's own type, unrelated to `google.protobuf.Timestamp`. On the wire it is a single `int64`, not a `{seconds, nanos}` message, so a non-protobus consumer reading the field sees milliseconds. That is deliberate — it is cheaper and it survives a peer that knows nothing about custom types — but it is not interchangeable with the well-known type.

---

## `CustomType` reference

A dataclass with **four required fields and one optional** ([`protobus/custom_types.py`](../../protobus/custom_types.py)). `ICustomType` is an alias for the TypeScript name.

| Field | Type | Meaning |
|---|---|---|
| `name` | `str` | the token used in `.proto` files. Lowercase, so it reads like a built-in scalar |
| `wire_type` | see below | how the value is actually encoded |
| `encode` | `Callable[[Any], bytes \| int \| str \| float]` | application value → wire value |
| `decode` | `Callable[[Any], T]` | wire value → application value |
| `py_type` | `str`, default `"Any"` | the Python type name emitted by the generator, verbatim. `ts_type` is a read-only alias |

**The allowed `wire_type` values, exactly:**

```
"bytes" | "int64" | "uint64" | "string" | "int32" | "uint32" | "double"
```

`bytes` is the most flexible and the one `bigint` reaches for; `string` is the easiest to debug, because a malformed value is readable in the RabbitMQ management UI.

Custom types work as map values, both directly and inside a message held in a map:

```protobuf
syntax = "proto3";
package Wallet;

message Balances {
    map<string, bigint> by_address = 1;   // converted per value
    map<string, Holding> holdings  = 2;   // converted per value, recursively
}

message Holding {
    bigint amount = 1;
}
```

Map keys are untouched; an empty map stays empty.

Custom types nest. A `bigint` three messages deep round-trips correctly, including inside self-referential messages — `test_round_trips_a_bigint_one_two_and_three_levels_deep` and `test_encodes_a_self_referential_message` pin it.

---

## Cross-language

The wrapper-message encoding is shared with the TypeScript port, so a `bigint` or `timestamp` written by one is read by the other — [`tests/integration/test_cross_language.py`](../../tests/integration/test_cross_language.py) round-trips both through a TypeScript server. A custom type of your own interoperates the same way provided both sides register the same `name` with the same `wire_type` and agree on the bytes; `py_type` and `tsType` are local to each port's code generator and never travel.

---

<div align="center">

**[← Errors](./errors.md)** · **[Docs index](../README.md)** · **[Context →](./api/context.md)**

</div>
