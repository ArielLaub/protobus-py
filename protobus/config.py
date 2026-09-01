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

    # ---- Message priority -------------------------------------------------
    # Three rungs, deliberately. RabbitMQ builds internal structures per
    # priority level, so a large x-max-priority costs real memory and
    # throughput; the broker's own guidance is to stay in single digits. Two
    # rungs cover the motivating case (control traffic jumping its own bulk
    # fan-out) and the third costs nothing measurable while saving a breaking
    # change later.
    #
    # PRIORITY_NORMAL is 0, which is also what RabbitMQ assumes for a message
    # published with no priority property at all — so an old publisher and a
    # new one that passes PRIORITY_NORMAL sort identically.
    PRIORITY_NORMAL = 0
    PRIORITY_HIGH = 1
    PRIORITY_CONTROL = 2

    # The value to pass as max_priority to make the constants above usable.
    RECOMMENDED_MAX_PRIORITY = 2
