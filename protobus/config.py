"""Configuration module for Protobus."""

import os


class Config:
    """Configuration class with environment variable support."""

    @staticmethod
    def bus_exchange_name() -> str:
        """Get the main bus exchange name."""
        return os.environ.get("BUS_EXCHANGE_NAME", "proto.bus")

    @staticmethod
    def callbacks_exchange_name() -> str:
        """Get the callbacks exchange name."""
        return os.environ.get("CALLBACKS_EXCHANGE_NAME", "proto.bus.callback")

    @staticmethod
    def events_exchange_name() -> str:
        """Get the events exchange name."""
        return os.environ.get("EVENTS_EXCHANGE_NAME", "proto.bus.events")

    @staticmethod
    def message_processing_timeout() -> int:
        """Get the message processing timeout in milliseconds."""
        timeout = os.environ.get("MESSAGE_PROCESSING_TIMEOUT")
        return int(timeout) if timeout else 600000

    @staticmethod
    def rpc_call_timeout() -> int:
        """
        Get the caller-side RPC deadline in milliseconds.

        This is the budget for a whole round trip and belongs to the *caller*.
        It is deliberately not message_processing_timeout(), which is the
        *server's* budget for running one handler and defaults to ten minutes:
        using it here meant a call to a service that was scaled to zero, or
        simply absent, blocked the caller for ten minutes.
        """
        timeout = os.environ.get("RPC_CALL_TIMEOUT_MS")
        return int(timeout) if timeout else 30000

    @staticmethod
    def amqp_heartbeat_seconds() -> int:
        """
        Get the AMQP heartbeat interval in seconds.

        Nothing set one before, so the interval was whatever the broker
        proposed — 60 seconds on RabbitMQ, and detection of a peer that
        vanished without closing its socket takes two missed intervals. For all
        of that time the connection reports itself healthy and publishes go
        into a dead socket. A heartbeat already present in the URL is the
        caller being explicit and is left alone.

        Set to 0 to disable heartbeats entirely.
        """
        value = os.environ.get("AMQP_HEARTBEAT_SECONDS")
        return int(value) if value is not None else 30

    @staticmethod
    def stream_max_buffered_chunks() -> int:
        """
        Get the maximum number of undelivered chunks a streaming call may hold.

        A producer that outruns its consumer would otherwise buffer without
        limit in the dispatcher's chunk queue.
        """
        value = os.environ.get("STREAM_MAX_BUFFERED_CHUNKS")
        return int(value) if value else 256

    @staticmethod
    def stream_idle_timeout() -> int:
        """
        Get the streaming idle timeout in milliseconds.

        A streaming call raises StreamTimeoutError if no chunk arrives within
        this window. Streaming calls do NOT use message_processing_timeout —
        a legitimate stream can take far longer than any single chunk gap.
        """
        timeout = os.environ.get("STREAM_IDLE_TIMEOUT_MS")
        return int(timeout) if timeout else 60000

    # Headers used by the streaming wire protocol. See docs/advanced/streaming.md.
    HEADER_FINAL = "x-protobus-final"
    HEADER_SEQ = "x-protobus-seq"
