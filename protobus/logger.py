"""
Logging for Protobus.

Two surfaces, mirroring the TypeScript port:

- ``Logger`` — the plain string logger every module uses. Level-filtered
  before the sink sees anything, so a custom sink installed with
  ``set_logger()`` never receives suppressed lines.
- ``Log`` — the structured counterpart. Describes what happened as a
  ``LogRecord``; a sink implementing ``log(record)`` receives the record, a
  plain one receives ``format_log_record(record)`` on its matching method.

Debug is **off by default**. Payload-level material only ever reaches a line
through the opt-in diagnostics serializer, which is never invoked unless the
application installs one.
"""

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit


@runtime_checkable
class ILogger(Protocol):
    """The sink interface: four severity methods taking a message."""

    def info(self, message: Any) -> None: ...

    def warn(self, message: Any) -> None: ...

    def debug(self, message: Any) -> None: ...

    def error(self, message: Any) -> None: ...


class LogLevel(IntEnum):
    """Severity threshold. Lower values are more verbose."""

    Debug = 10
    Info = 20
    Warn = 30
    Error = 40
    Silent = 100


def _level_from_env() -> LogLevel:
    value = (os.environ.get("LOG_LEVEL") or "").strip().lower()
    if value == "debug":
        return LogLevel.Debug
    if value == "info":
        return LogLevel.Info
    if value in ("warn", "warning"):
        return LogLevel.Warn
    if value == "error":
        return LogLevel.Error
    if value in ("silent", "off", "none"):
        return LogLevel.Silent
    return LogLevel.Info


_current_level: LogLevel = _level_from_env()


def set_level(level: LogLevel) -> None:
    """Set the minimum severity that will be emitted."""
    global _current_level
    _current_level = LogLevel(level)


def get_level() -> LogLevel:
    """Current minimum severity."""
    return _current_level


# Aliases matching the package-root export names.
set_log_level = set_level
get_log_level = get_level


class DefaultLogger:
    """
    Default sink, backed by the ``protobus`` stdlib logger.

    The stdlib logger is left at its most permissive level: filtering is done
    by ``Logger`` before anything reaches here, so a second gate would only
    hide lines the framework already decided to emit.
    """

    def __init__(self, name: str = "protobus"):
        self._logger = logging.getLogger(name)
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(
                logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            )
            self._logger.addHandler(handler)
            self._logger.propagate = False
        self._logger.setLevel(logging.DEBUG)

    def info(self, message: Any) -> None:
        self._logger.info(message)

    def debug(self, message: Any) -> None:
        self._logger.debug(message)

    def warn(self, message: Any) -> None:
        self._logger.warning(message)

    def error(self, message: Any) -> None:
        self._logger.error(message)


_sink: ILogger = DefaultLogger()


def set_logger(new_logger: ILogger) -> None:
    """Install a custom sink."""
    global _sink
    _sink = new_logger


def get_logger() -> ILogger:
    """The installed sink."""
    return _sink


# TS parity alias.
set = set_logger  # noqa: A001


class Logger:
    """Level-filtered string logger. Every module logs through this."""

    @staticmethod
    def debug(message: Any) -> None:
        if _current_level <= LogLevel.Debug:
            _sink.debug(message)

    @staticmethod
    def info(message: Any) -> None:
        if _current_level <= LogLevel.Info:
            _sink.info(message)

    @staticmethod
    def warn(message: Any) -> None:
        if _current_level <= LogLevel.Warn:
            _sink.warn(message)

    @staticmethod
    def error(message: Any) -> None:
        if _current_level <= LogLevel.Error:
            _sink.error(message)


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

LogLevelName = str  # 'debug' | 'info' | 'warn' | 'error'
LogOutcome = str  # 'ok' | 'confirmed' | 'failed' | 'timeout' | 'retried' | 'rejected' | 'dropped' | 'unroutable'

TEXT_FIELDS = (
    "message_type",
    "message_id",
    "correlation_id",
    "service",
    "method",
    "queue",
    "exchange",
    "routing_key",
    "error_code",
    "error_name",
    "outcome",
)
NUMERIC_FIELDS = ("size_bytes", "duration_ms", "attempt")

FIELD_MAX_LENGTH = 256
MESSAGE_MAX_LENGTH = 1024

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass
class LogRecord:
    """
    One framework log line as data.

    Every field is either framework-generated or a low-cardinality identifier:
    connection URLs, headers, payloads and raw broker error strings are
    deliberately absent. The only route for any of those is ``diagnostics``.
    """

    level: LogLevelName
    timestamp: str
    operation: str
    message: str
    component: str = "protobus"
    message_type: Optional[str] = None
    message_id: Optional[str] = None
    correlation_id: Optional[str] = None
    service: Optional[str] = None
    method: Optional[str] = None
    queue: Optional[str] = None
    exchange: Optional[str] = None
    routing_key: Optional[str] = None
    error_code: Optional[str] = None
    error_name: Optional[str] = None
    outcome: Optional[LogOutcome] = None
    size_bytes: Optional[int] = None
    duration_ms: Optional[float] = None
    attempt: Optional[int] = None
    diagnostics: Any = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "component": self.component,
            "level": self.level,
            "timestamp": self.timestamp,
            "operation": self.operation,
            "message": self.message,
        }
        for key in TEXT_FIELDS + NUMERIC_FIELDS:
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.diagnostics is not None:
            out["diagnostics"] = self.diagnostics
        return out


@dataclass
class LogDiagnostics:
    """Raw material offered to the diagnostics serializer, never logged as-is."""

    payload: Any = None
    headers: Optional[Dict[str, Any]] = None
    error: Any = None
    extra: Dict[str, Any] = field(default_factory=dict)


DiagnosticsSerializer = Callable[[LogDiagnostics, LogRecord], Any]


@runtime_checkable
class IStructuredLogger(Protocol):
    """A sink that accepts records. Works everywhere ILogger does."""

    def info(self, message: Any) -> None: ...

    def warn(self, message: Any) -> None: ...

    def debug(self, message: Any) -> None: ...

    def error(self, message: Any) -> None: ...

    def log(self, record: LogRecord) -> None: ...


def is_structured_logger(candidate: Any) -> bool:
    """True when the sink can accept records rather than only strings."""
    return callable(getattr(candidate, "log", None))


_diagnostics_serializer: Optional[DiagnosticsSerializer] = None


def set_diagnostics_serializer(serializer: Optional[DiagnosticsSerializer]) -> None:
    """
    Install (or, with None, remove) the diagnostics serializer.

    Off by default: with no serializer, no call site's payload thunk is ever
    invoked and ``diagnostics`` never appears on a record.
    """
    global _diagnostics_serializer
    _diagnostics_serializer = serializer


def get_diagnostics_serializer() -> Optional[DiagnosticsSerializer]:
    return _diagnostics_serializer


def _sanitize_value(value: Any, max_length: int) -> Optional[str]:
    """
    Normalise one field value, or drop it.

    Objects are rejected rather than stringified: a caller that hands an
    object to a scalar field is most likely handing over a payload. Control
    characters are collapsed so a value cannot forge a second log line, and
    long values are truncated so one field cannot flood the log.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, str):
        text = value
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return None
        text = str(value)
    else:
        return None
    text = _CONTROL_CHARS.sub(" ", text).strip()
    if not text:
        return None
    return text[:max_length] if len(text) > max_length else text


def _sanitize_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def _build_record(level: LogLevelName, message: str, fields: Dict[str, Any]) -> LogRecord:
    record = LogRecord(
        level=level,
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        operation=_sanitize_value(fields.get("operation"), FIELD_MAX_LENGTH) or "unknown",
        message=_sanitize_value(message, MESSAGE_MAX_LENGTH) or "",
    )
    for key in TEXT_FIELDS:
        value = _sanitize_value(fields.get(key), FIELD_MAX_LENGTH)
        if value is not None:
            setattr(record, key, value)
    for key in NUMERIC_FIELDS:
        value = _sanitize_number(fields.get(key))
        if value is not None:
            setattr(record, key, value)
    return record


def _stringify_diagnostics(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except Exception:
        return "<unserializable>"


def format_log_record(record: LogRecord) -> str:
    """
    Render a record as the single human-readable line a string-only sink gets.
    Exported so an application formatting records itself can match the
    framework's default output.
    """
    parts = []
    for key in TEXT_FIELDS + NUMERIC_FIELDS:
        value = getattr(record, key)
        if value is not None:
            parts.append(f"{key}={value}")
    if record.diagnostics is not None:
        parts.append(f"diagnostics={_stringify_diagnostics(record.diagnostics)}")
    detail = f" ({' '.join(parts)})" if parts else ""
    return f"[{record.component}] {record.operation}: {record.message}{detail}"


def _write_text(target: Any, level: LogLevelName, text: str) -> None:
    getattr(target, level)(text)


def _emit(level: LogLevelName, threshold: LogLevel, message: str, fields: Dict[str, Any]) -> None:
    if _current_level > threshold:
        return

    record = _build_record(level, message, fields)

    serializer = _diagnostics_serializer
    diagnostics = fields.get("diagnostics")
    if serializer is not None and callable(diagnostics):
        try:
            extra = serializer(diagnostics(), record)
            if extra is not None:
                record.diagnostics = extra
        except Exception:
            # A failing hook must not take down the operation being logged.
            pass

    target = _sink
    if is_structured_logger(target):
        try:
            target.log(record)  # type: ignore[attr-defined]
            return
        except Exception:
            # A structured sink that throws degrades to the string path.
            pass

    _write_text(target, level, format_log_record(record))


class Log:
    """
    Structured counterpart to ``Logger``.

    A call site looks like::

        Log.info('published request',
                 operation='publish',
                 message_type='example.Service.DoThing',
                 correlation_id=correlation_id,
                 size_bytes=len(content),
                 outcome='confirmed',
                 diagnostics=lambda: LogDiagnostics(payload=decoded))
    """

    @staticmethod
    def debug(message: str, **fields: Any) -> None:
        _emit("debug", LogLevel.Debug, message, fields)

    @staticmethod
    def info(message: str, **fields: Any) -> None:
        _emit("info", LogLevel.Info, message, fields)

    @staticmethod
    def warn(message: str, **fields: Any) -> None:
        _emit("warn", LogLevel.Warn, message, fields)

    @staticmethod
    def error(message: str, **fields: Any) -> None:
        _emit("error", LogLevel.Error, message, fields)


# ---------------------------------------------------------------------------
# URL redaction
# ---------------------------------------------------------------------------


def redact_url(url: Any) -> str:
    """
    Strip credentials out of a broker URL so it is safe to log.

    The password is replaced with ``***``; username, host, port and vhost are
    kept because they are what makes the line useful. Anything that does not
    parse as a URL is reported as ``<redacted>`` rather than passed through —
    an unparseable string may still be a credential.
    """
    if not url:
        return str(url)
    if not isinstance(url, str):
        return "<redacted>"
    try:
        parts = urlsplit(url)
        if not parts.scheme or "://" not in url:
            return "<redacted>"
        if parts.password is None:
            return url
        netloc = parts.netloc
        userinfo, _, hostport = netloc.rpartition("@")
        user, _, _ = userinfo.partition(":")
        netloc = f"{user}:***@{hostport}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "<redacted>"
