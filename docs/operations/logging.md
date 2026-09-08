# Logging

> Levels, your own sink, structured records, and the one hook that can let payloads out of the process.

**Read this if** you are wiring protobus into your log pipeline, or you turned on debug logging and got nothing.

| | |
|---|---|
| **Prerequisites** | [Getting Started](../guide/getting-started.md) |
| **Next** | [Security](./security.md) — what a log line must never carry · [Troubleshooting](./troubleshooting.md) |
| **Source** | [`protobus/logger.py`](../../protobus/logger.py) |

**On this page** — [Levels](#levels-first) · [Your own sink](#install-your-own-sink) · [Structured records](#structured-records) · [What a record never carries](#what-a-record-never-carries) · [Payload diagnostics](#opt-in-payload-diagnostics) · [Your own records](#emitting-your-own-records) · [Testing](#silencing-it-in-tests)

---

## Levels first

> [!IMPORTANT]
> **Installing a sink does not turn on debug logging.** The level filter is
> applied *before* the sink is called ([`protobus/logger.py`](../../protobus/logger.py)),
> so a logger with a perfectly good `debug` method receives nothing until the
> level allows it. This is the single most common confusion about protobus
> logging.

| Level | Value | Emitted when the level is |
|---|---:|---|
| `LogLevel.Debug` | 10 | `Debug` |
| `LogLevel.Info` | 20 | `Debug`, `Info` |
| `LogLevel.Warn` | 30 | `Debug`, `Info`, `Warn` |
| `LogLevel.Error` | 40 | anything but `Silent` |
| `LogLevel.Silent` | 100 | never |

The default is `Info`. Set it either way:

```bash
LOG_LEVEL=debug python server.py   # debug | info | warn | error | silent
```

```python
from protobus import LogLevel, get_log_level, set_log_level

set_log_level(LogLevel.Debug)
assert get_log_level() == LogLevel.Debug
```

`LOG_LEVEL` is read once, at import; `set_log_level` changes it at any time.

> [!CAUTION]
> Debug is off by default on purpose. The default sink writes to **stderr**, so
> anything logged at that level reaches whatever aggregates your process
> output. Turn it on deliberately, and read
> [Security](./security.md) first if the environment is production.

---

## Install your own sink

`ILogger` is four methods. Anything satisfying it can receive protobus's lines:

```python
from protobus import set_logger


class Sink:
    def debug(self, message): print("[DEBUG]", message)
    def info(self, message): print("[INFO]", message)
    def warn(self, message): print("[WARN]", message)
    def error(self, message): print("[ERROR]", message)


set_logger(Sink())
```

Adapting the standard library, or structlog, or loguru, is the same four lines.
The only mismatch is the name: protobus says `warn`, `logging` says `warning`.

<details>
<summary><code>logging</code>, structlog and loguru adapters</summary>

```python
import logging

import structlog
from loguru import logger as loguru_logger

from protobus import set_logger


class StdlibLogger:
    def __init__(self, name: str = "protobus"):
        self._log = logging.getLogger(name)

    def debug(self, message): self._log.debug(message)
    def info(self, message): self._log.info(message)
    def warn(self, message): self._log.warning(message)
    def error(self, message): self._log.error(message)


set_logger(StdlibLogger())


class StructlogLogger:
    def __init__(self):
        self._log = structlog.get_logger("protobus")

    def debug(self, message): self._log.debug(message)
    def info(self, message): self._log.info(message)
    def warn(self, message): self._log.warning(message)
    def error(self, message): self._log.error(message)


class LoguruLogger:
    def debug(self, message): loguru_logger.debug(message)
    def info(self, message): loguru_logger.info(message)
    def warn(self, message): loguru_logger.warning(message)
    def error(self, message): loguru_logger.error(message)
```

</details>

> [!TIP]
> **Do not reimplement level filtering in your sink.** Protobus already applies
> it, and a second filter downstream only makes `set_log_level` look broken. Set
> the level on protobus; let your logger do transport and formatting.

The default sink, `DefaultLogger`, is a thin wrapper over a `logging` logger named
`protobus`. It attaches its own stderr handler and sets `propagate = False` only
when that logger has no handlers yet — so configure the `protobus` logger
*before* importing protobus and your handlers are used instead.

---

## Structured records

Protobus can emit its own lines as structured records instead of free text. It is
opt-in and additive: do nothing, or install a plain `ILogger`, and the
human-readable output is unchanged.

Implement `log(record)` on your sink. `IStructuredLogger` is `ILogger` plus that
one method, so one object satisfies both:

```python
import json
import sys

from protobus import LogRecord, set_logger


class JsonSink:
    def log(self, record: LogRecord) -> None:
        sys.stdout.write(json.dumps(record.to_dict()) + "\n")

    # Still required: not every framework line is structured yet, and these
    # keep working for anything that logs a plain string.
    def debug(self, message): print(message)
    def info(self, message): print(message)
    def warn(self, message): print(message, file=sys.stderr)
    def error(self, message): print(message, file=sys.stderr)


set_logger(JsonSink())
```

<details>
<summary>The full <code>LogRecord</code> shape</summary>

```python
@dataclass
class LogRecord:
    level: str                     # 'debug' | 'info' | 'warn' | 'error'
    timestamp: str                 # ISO 8601
    operation: str                 # 'publish' | 'consume' | 'connect' | ...
    message: str                   # human-readable summary
    component: str = "protobus"
    message_type: str | None       # e.g. 'example.Service.DoThing'
    message_id: str | None
    correlation_id: str | None
    service: str | None
    method: str | None
    queue: str | None
    exchange: str | None
    routing_key: str | None
    error_code: str | None         # framework-classified, never a broker string
    error_name: str | None         # exception class name, no message text
    outcome: str | None            # 'ok' | 'confirmed' | 'failed' | 'timeout'
                                   # | 'retried' | 'rejected' | 'dropped' | 'unroutable'
    size_bytes: int | None
    duration_ms: float | None
    attempt: int | None
    diagnostics: Any = None        # only ever what your serializer returns

    def to_dict(self) -> dict: ... # set fields only, snake_case keys
```

</details>

One record, as `to_dict()` renders it:

```json
{
  "component": "protobus",
  "level": "info",
  "timestamp": "2026-01-01T00:00:00.000Z",
  "operation": "publish",
  "message": "published request",
  "message_type": "example.Service.DoThing",
  "message_id": "01H...",
  "correlation_id": "8f3c...",
  "size_bytes": 1234,
  "outcome": "confirmed"
}
```

Field names are snake_case here and camelCase in the TypeScript port's records;
the vocabulary is otherwise the same, so one dashboard can read both with a
key mapping.

A sink **without** `log()` receives the same content rendered as one line on the
matching severity method:

```
[protobus] publish: published request (message_type=example.Service.DoThing correlation_id=8f3c... outcome=confirmed size_bytes=1234)
```

`format_log_record(record)` is exported if you want to produce that exact text
yourself. Level filtering happens before either path, so `set_log_level()` and
`LOG_LEVEL` behave identically for structured and string output, and a
suppressed line reaches neither. A `log()` that raises degrades to the string
path rather than losing the line.

---

## What a record never carries

Connection URLs, message headers, message bodies, protobuf-decoded values and
broker-supplied error strings are not in the field set, and fields outside the
list above are dropped rather than passed through.

Values are normalised before they reach your sink: control characters collapse to
spaces so a value cannot forge a second log line, values are truncated (256
characters; 1024 for `message`), and an object handed to a scalar field is
dropped rather than stringified.

The only route for payload-level material is `diagnostics`, which stays absent
unless you install a serializer.

---

## Opt-in payload diagnostics

Call sites can offer payload material lazily. It is assembled only once you have
installed a serializer, and what survives into the record is entirely your
decision — **the framework applies no redaction to the value you return.**

```python
from protobus import LogDiagnostics, LogRecord, set_diagnostics_serializer


# Log field names only, never values.
def field_names_only(diagnostics: LogDiagnostics, record: LogRecord):
    if not isinstance(diagnostics.payload, dict):
        return None
    return {"fields": sorted(diagnostics.payload)}


set_diagnostics_serializer(field_names_only)
```

The serializer receives the assembled `LogDiagnostics` (`payload`, `headers`,
`error`, plus whatever the call site adds under `extra`) and the record it is
about to be attached to:

```python
import os

from protobus import set_diagnostics_serializer


def consume_payloads_outside_production(diagnostics, record):
    # Full payloads for one operation, in one environment, and nothing else.
    if os.environ.get("ENV") == "production":
        return None
    if record.operation != "consume":
        return None
    return {"payload": diagnostics.payload}


set_diagnostics_serializer(consume_payloads_outside_production)

set_diagnostics_serializer(None)   # back off; nothing is assembled again
```

Returning `None` omits the field. A serializer that raises is ignored and the
line is still emitted without diagnostics.

> [!CAUTION]
> This hook is the point at which payloads can leave the process. Whatever you
> return is passed to your sink as-is — redact it there.

---

## Emitting your own records

`Log` is the structured counterpart to `Logger`, and your services can use it for
their own lines:

```python
from protobus import Log, LogDiagnostics


def record_publish(correlation_id: str, content: bytes, decoded: dict) -> None:
    Log.info(
        "published request",
        operation="publish",
        message_type="example.Service.DoThing",
        correlation_id=correlation_id,
        size_bytes=len(content),
        outcome="confirmed",
        diagnostics=lambda: LogDiagnostics(payload=decoded),   # read only if a serializer is installed
    )
```

The `diagnostics` thunk is not invoked unless a serializer is installed **and**
the line passes the level filter, so building it costs nothing when payload
logging is off. Unknown keyword fields are dropped, not passed through.

---

## Silencing it in tests

```python
from protobus import LogLevel, set_log_level, set_logger


class Silent:
    def debug(self, message): pass
    def info(self, message): pass
    def warn(self, message): pass
    def error(self, message): pass


def quiet_protobus() -> None:
    set_logger(Silent())
    set_log_level(LogLevel.Silent)
```

Either alone is enough; both together also stop anything reaching the default
sink if a later test replaces the logger. This repository's own
[`tests/conftest.py`](../../tests/conftest.py) sets `LOG_LEVEL=warn` for the
whole suite. See [Testing](../guide/testing.md).

---

<div align="center">

**[← Queue Migration](./queue-migration.md)** · **[Docs index](../README.md)** · **[Security →](./security.md)**

</div>
