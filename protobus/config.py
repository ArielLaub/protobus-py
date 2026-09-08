"""
Configuration, read from the environment on every access.

Every getter is a ``staticmethod`` rather than a module constant so that a test
(or an operator restarting a process) that changes the environment is picked
up. Integer parsing is strict: ``parseInt``-style tolerance of trailing garbage
turned ``6oo000`` into ``NaN`` in the TypeScript port, and a NaN timeout fires
immediately — so every message was flagged as timed out. Anything malformed
falls back to the default here instead.
"""

import os
import re
from typing import Dict, Optional, Tuple

# Memo of parsed integer env vars: raw string -> parsed value, so a variable
# changed at runtime is re-parsed while the per-message hot path pays for a
# dict lookup rather than a regex.
_int_cache: Dict[str, Tuple[Optional[str], int]] = {}

_DIGITS = re.compile(r"^\d+$")


def env_int(name: str, fallback: int) -> int:
    """
    Parse a positive-integer environment variable, falling back on anything
    malformed, empty, zero or negative.
    """
    raw = os.environ.get(name)
    hit = _int_cache.get(name)
    if hit is not None and hit[0] == raw:
        return hit[1]

    value = fallback
    if raw is not None:
        text = raw.strip()
        if text and _DIGITS.match(text):
            parsed = int(text)
            if parsed > 0:
                value = parsed

    _int_cache[name] = (raw, value)
    return value


def env_bool(name: str, fallback: bool) -> bool:
    """Parse a boolean environment variable. Only an explicit value counts."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return fallback
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return fallback


class Config:
    """Environment-backed configuration. Mirrors the TypeScript ``Config``."""

    # ------------------------------------------------------------------
    # Error exposure
    # ------------------------------------------------------------------

    @staticmethod
    def expose_internal_errors() -> bool:
        """
        Send the message of an *unhandled* service error back to the caller.

        On by default. A protobus caller is another of your own services,
        already inside the trust boundary and holding the broker credentials,
        so an error message travelling service to service is an internal
        detail moving between components that already trust each other.

        Set ``PROTOBUS_EXPOSE_INTERNAL_ERRORS=false`` where that assumption
        does not hold — chiefly a service that forwards protobus errors onward
        to an untrusted client. Callers then get a generic message plus the
        correlation id, and the real error still goes to the service's log.

        ``HandledError`` is unaffected either way: raising one is an explicit
        decision to tell the caller something, so its message always crosses.
        """
        return env_bool("PROTOBUS_EXPOSE_INTERNAL_ERRORS", True)

    # ------------------------------------------------------------------
    # Exchange names
    # ------------------------------------------------------------------

    @staticmethod
    def bus_exchange_name() -> str:
        return os.environ.get("BUS_EXCHANGE_NAME") or "proto.bus"

    @staticmethod
    def callbacks_exchange_name() -> str:
        return os.environ.get("CALLBACKS_EXCHANGE_NAME") or "proto.bus.callback"

    @staticmethod
    def cancel_exchange_name() -> str:
        """
        Fanout exchange carrying stream-cancellation notices.

        Fanout, not topic: a cancel has to reach the one process holding that
        correlation id, and the caller has no way to know which replica that
        is. Every service instance binds its own exclusive queue, hears every
        cancel, and ignores the ones it does not own.
        """
        return os.environ.get("CANCEL_EXCHANGE_NAME") or "proto.bus.cancel"

    @staticmethod
    def events_exchange_name() -> str:
        return os.environ.get("EVENTS_EXCHANGE_NAME") or "proto.bus.events"

    # ------------------------------------------------------------------
    # Timeouts and bounds
    # ------------------------------------------------------------------

    @staticmethod
    def message_processing_timeout() -> int:
        """Server-side budget for one handler, in milliseconds. Default 10 min."""
        return env_int("MESSAGE_PROCESSING_TIMEOUT", 600000)

    @staticmethod
    def rpc_call_timeout_ms() -> int:
        """
        How long a unary RPC caller waits for a reply before raising
        ``RpcTimeoutError``.

        Defaults to the same ten minutes as ``message_processing_timeout`` so
        deployments with legitimately slow handlers keep working; override per
        call with ``timeout_ms``.
        """
        return env_int("RPC_CALL_TIMEOUT_MS", 600000)

    @staticmethod
    def stream_idle_timeout_ms() -> int:
        """
        Idle timeout for streaming calls, in milliseconds. A stream raises
        ``StreamTimeoutError`` when no chunk arrives within this window. The
        processing timeout does NOT apply to streams.
        """
        return env_int("STREAM_IDLE_TIMEOUT_MS", 60000)

    @staticmethod
    def default_prefetch() -> int:
        """Prefetch for late-ack consumers that do not set one."""
        return env_int("DEFAULT_PREFETCH", 1)

    @staticmethod
    def publish_confirm_timeout_ms() -> int:
        """
        How long a publish waits for its broker confirm before raising
        ``PublishConfirmTimeoutError`` — an AMBIGUOUS outcome, not a failure.
        """
        return env_int("PUBLISH_CONFIRM_TIMEOUT_MS", 30000)

    @staticmethod
    def heartbeat_seconds() -> int:
        """
        AMQP heartbeat interval in seconds. A URL that already carries
        ``?heartbeat=`` wins, which is also how heartbeats are disabled.
        """
        return env_int("AMQP_HEARTBEAT_SECONDS", 30)

    @staticmethod
    def connection_ready_timeout_ms() -> int:
        """How long a publisher parked on a reconnection waits before giving up."""
        return env_int("CONNECTION_READY_TIMEOUT_MS", 30000)

    @staticmethod
    def max_outstanding_confirms() -> int:
        """Maximum publishes awaiting a broker confirm on one channel at a time."""
        return env_int("MAX_OUTSTANDING_CONFIRMS", 256)

    @staticmethod
    def stream_max_buffered_chunks() -> int:
        """Upper bound on chunks buffered for one streaming call."""
        return env_int("STREAM_MAX_BUFFERED_CHUNKS", 1024)

    @staticmethod
    def stream_max_buffered_bytes() -> int:
        """Upper bound on buffered bytes for one streaming call. Default 64 MiB."""
        return env_int("STREAM_MAX_BUFFERED_BYTES", 64 * 1024 * 1024)

    @staticmethod
    def stream_max_total_buffered_bytes() -> int:
        """Upper bound on buffered bytes across all streams. Default 256 MiB."""
        return env_int("STREAM_MAX_TOTAL_BUFFERED_BYTES", 256 * 1024 * 1024)

    # ------------------------------------------------------------------
    # Constants
    # ------------------------------------------------------------------

    # Named message-priority levels, matching the TypeScript port. NORMAL is 0
    # because that is what RabbitMQ assigns a message with no priority at all.
    PRIORITY_NORMAL = 0
    PRIORITY_HIGH = 1
    PRIORITY_CONTROL = 2

    # The ``max_priority`` to declare a priority queue with, for the levels
    # above. RabbitMQ maintains structures per level, so keep it small.
    RECOMMENDED_MAX_PRIORITY = 2

    # Headers used by the streaming wire protocol. See docs/advanced/streaming.md.
    HEADER_FINAL = "x-protobus-final"
    HEADER_SEQ = "x-protobus-seq"

    # ------------------------------------------------------------------
    # Legacy aliases (1.x names). Kept so existing callers keep working.
    # ------------------------------------------------------------------

    @staticmethod
    def rpc_call_timeout() -> int:
        return Config.rpc_call_timeout_ms()

    @staticmethod
    def stream_idle_timeout() -> int:
        return Config.stream_idle_timeout_ms()

    @staticmethod
    def amqp_heartbeat_seconds() -> int:
        return Config.heartbeat_seconds()
