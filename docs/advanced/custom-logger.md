# Custom Logger

Protobus uses a pluggable logging system that can be customized to integrate with your application's logging infrastructure.

## Default Logger

By default, Protobus uses Python's built-in logging module:

```python
import logging

# Default setup
logger = logging.getLogger("protobus")
logger.setLevel(logging.DEBUG)
```

## Using the Logger

### From Library Code

```python
from protobus.logger import log

# Available log levels
log.info("Service started")
log.debug("Processing message", {"message_id": "123"})
log.warn("Connection unstable")
log.error("Failed to process", {"error": str(e)})
```

### From Your Services

```python
from protobus.logger import log

class MyService(MessageService):
    async def my_method(self, data, actor, correlation_id):
        log.info(f"Processing request {correlation_id}")
        # ... implementation
```

## Custom Logger Implementation

### Logger Interface

Create a custom logger by implementing the `ILogger` protocol:

```python
from typing import Any, Optional

class ILogger:
    def info(self, message: str, meta: Optional[dict[str, Any]] = None) -> None: ...
    def debug(self, message: str, meta: Optional[dict[str, Any]] = None) -> None: ...
    def warn(self, message: str, meta: Optional[dict[str, Any]] = None) -> None: ...
    def error(self, message: str, meta: Optional[dict[str, Any]] = None) -> None: ...
```

### Example: Structured JSON Logger

```python
import json
import sys
from datetime import datetime
from typing import Any, Optional

class JSONLogger:
    """Structured JSON logger for production environments."""

    def __init__(self, service_name: str = "protobus"):
        self.service_name = service_name

    def _log(self, level: str, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": level,
            "service": self.service_name,
            "message": message,
            **(meta or {})
        }
        print(json.dumps(entry), file=sys.stderr)

    def info(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        self._log("INFO", message, meta)

    def debug(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        self._log("DEBUG", message, meta)

    def warn(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        self._log("WARN", message, meta)

    def error(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        self._log("ERROR", message, meta)
```

### Example: Integration with Loguru

```python
from loguru import logger as loguru_logger
from typing import Any, Optional

class LoguruAdapter:
    """Adapter for using Loguru with Protobus."""

    def info(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        loguru_logger.bind(**meta or {}).info(message)

    def debug(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        loguru_logger.bind(**meta or {}).debug(message)

    def warn(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        loguru_logger.bind(**meta or {}).warning(message)

    def error(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        loguru_logger.bind(**meta or {}).error(message)
```

### Example: Integration with structlog

```python
import structlog
from typing import Any, Optional

class StructlogAdapter:
    """Adapter for using structlog with Protobus."""

    def __init__(self):
        self.logger = structlog.get_logger()

    def info(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        self.logger.info(message, **(meta or {}))

    def debug(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        self.logger.debug(message, **(meta or {}))

    def warn(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        self.logger.warning(message, **(meta or {}))

    def error(self, message: str, meta: Optional[dict[str, Any]] = None) -> None:
        self.logger.error(message, **(meta or {}))
```

## Setting a Custom Logger

### Using set_logger()

```python
from protobus.logger import set_logger

# Set your custom logger
custom_logger = JSONLogger(service_name="my-app")
set_logger(custom_logger)

# All Protobus logs now use your logger
```

### At Application Startup

```python
import asyncio
from protobus import Context
from protobus.logger import set_logger

async def main():
    # Configure logging first
    set_logger(JSONLogger(service_name="order-service"))

    # Then initialize context
    ctx = Context()
    await ctx.init("amqp://localhost")

    # ... rest of application

asyncio.run(main())
```

## Log Levels

### When Each Level is Used

| Level | Usage |
|-------|-------|
| `debug` | Detailed diagnostic information (message contents, queue operations) |
| `info` | General operational events (service started, connection established) |
| `warn` | Warning conditions (reconnecting, retry attempt) |
| `error` | Error conditions (failed to process, connection lost) |

### Controlling Log Output

```python
import logging

# Show only warnings and errors
logging.getLogger("protobus").setLevel(logging.WARNING)

# Show all logs including debug
logging.getLogger("protobus").setLevel(logging.DEBUG)
```

## Best Practices

### 1. Include Correlation IDs

```python
class CorrelationLogger:
    def __init__(self, base_logger):
        self.base = base_logger
        self.correlation_id = None

    def with_correlation(self, cid: str) -> "CorrelationLogger":
        new_logger = CorrelationLogger(self.base)
        new_logger.correlation_id = cid
        return new_logger

    def info(self, message: str, meta: Optional[dict] = None) -> None:
        meta = meta or {}
        if self.correlation_id:
            meta["correlation_id"] = self.correlation_id
        self.base.info(message, meta)

    # ... similar for other methods
```

### 2. Avoid Logging Sensitive Data

```python
def sanitize_meta(meta: dict) -> dict:
    """Remove sensitive fields from log metadata."""
    sensitive_keys = {"password", "token", "secret", "api_key", "credit_card"}
    return {
        k: "***REDACTED***" if k.lower() in sensitive_keys else v
        for k, v in meta.items()
    }

class SanitizedLogger:
    def __init__(self, base_logger):
        self.base = base_logger

    def info(self, message: str, meta: Optional[dict] = None) -> None:
        self.base.info(message, sanitize_meta(meta or {}))

    # ... similar for other methods
```

### 3. Use Contextual Logging in Services

```python
class MyService(MessageService):
    async def process_order(self, data, actor, correlation_id):
        # Log with context
        log.info("Processing order", {
            "order_id": data.get("id"),
            "actor": actor,
            "correlation_id": correlation_id
        })

        try:
            result = await self._do_work(data)
            log.info("Order processed", {
                "order_id": data.get("id"),
                "correlation_id": correlation_id
            })
            return result
        except Exception as e:
            log.error("Order processing failed", {
                "order_id": data.get("id"),
                "correlation_id": correlation_id,
                "error": str(e)
            })
            raise
```

---

See also: [Configuration](../configuration.md) | [Troubleshooting](../troubleshooting.md)
