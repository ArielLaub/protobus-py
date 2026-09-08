"""Shared pytest configuration."""

import os

import pytest

# Keep the log quiet unless a test opts in; the default sink writes to stderr.
os.environ.setdefault("LOG_LEVEL", "warn")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from the same environment: the Config getters read
    it on each access, so one test's override must not leak into the next."""
    for name in (
        "PUBLISH_CONFIRM_TIMEOUT_MS", "MAX_OUTSTANDING_CONFIRMS", "MESSAGE_PROCESSING_TIMEOUT",
        "RPC_CALL_TIMEOUT_MS", "STREAM_IDLE_TIMEOUT_MS", "STREAM_MAX_BUFFERED_CHUNKS",
        "STREAM_MAX_BUFFERED_BYTES", "STREAM_MAX_TOTAL_BUFFERED_BYTES", "DEFAULT_PREFETCH",
        "AMQP_HEARTBEAT_SECONDS", "CONNECTION_READY_TIMEOUT_MS", "PROTOBUS_EXPOSE_INTERNAL_ERRORS",
        "BUS_EXCHANGE_NAME", "CALLBACKS_EXCHANGE_NAME", "EVENTS_EXCHANGE_NAME", "CANCEL_EXCHANGE_NAME",
        "SHUTDOWN_DRAIN_TIMEOUT_MS", "PROTO_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
