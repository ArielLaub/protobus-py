"""Logger module for Protobus."""

import logging
from typing import Any, Protocol


class ILogger(Protocol):
    """Logger interface protocol."""

    def info(self, message: Any) -> None:
        """Log info message."""
        ...

    def warn(self, message: Any) -> None:
        """Log warning message."""
        ...

    def debug(self, message: Any) -> None:
        """Log debug message."""
        ...

    def error(self, message: Any) -> None:
        """Log error message."""
        ...


class DefaultLogger:
    """Default logger implementation using Python's logging module."""

    def __init__(self, name: str = "protobus"):
        self._logger = logging.getLogger(name)
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.DEBUG)

    def info(self, message: Any) -> None:
        """Log info message."""
        self._logger.info(message)

    def warn(self, message: Any) -> None:
        """Log warning message."""
        self._logger.warning(message)

    def debug(self, message: Any) -> None:
        """Log debug message."""
        self._logger.debug(message)

    def error(self, message: Any) -> None:
        """Log error message."""
        self._logger.error(message)


class _LoggerHolder:
    """Holder class for the global logger instance."""

    _instance: ILogger = DefaultLogger()

    @classmethod
    def get(cls) -> ILogger:
        return cls._instance

    @classmethod
    def set(cls, logger: ILogger) -> None:
        cls._instance = logger


class Logger:
    """Static logger class that delegates to the configured logger."""

    @staticmethod
    def info(message: Any) -> None:
        """Log info message."""
        _LoggerHolder.get().info(message)

    @staticmethod
    def warn(message: Any) -> None:
        """Log warning message."""
        _LoggerHolder.get().warn(message)

    @staticmethod
    def debug(message: Any) -> None:
        """Log debug message."""
        _LoggerHolder.get().debug(message)

    @staticmethod
    def error(message: Any) -> None:
        """Log error message."""
        _LoggerHolder.get().error(message)


def set_logger(logger: ILogger) -> None:
    """Set a custom logger implementation."""
    _LoggerHolder.set(logger)
