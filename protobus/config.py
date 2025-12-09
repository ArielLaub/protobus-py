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
