"""
Validation for RabbitMQ queue and message priorities.

Both of the values this module guards are encoded as a single AMQP octet, and
both fail badly and late if they are wrong:

- ``x-max-priority`` outside 1..255 is rejected by the broker at declare time
  with a 406 PRECONDITION_FAILED, which closes the channel. Protobus shares one
  connection across every listener in a process, so one bad value is a
  service-wide outage rather than a local error. Verified against RabbitMQ 3:
  ``x-max-priority: 300`` returns ``{max_value_exceeded,300}``.

- A message ``priority`` outside 0..255 raises a raw ``struct.error`` from
  inside the AMQP encoder ("'B' format requires 0 <= number <= 255") — and a
  *non-integer* one does not raise at all: aio-pika does ``int(priority)``, so
  ``1.5`` is silently stored as ``1``. The silent case is the dangerous one.

Validating here turns all of that into one clear ``InvalidPriorityError``, and
keeps this port's behaviour identical to the TypeScript port's, which validates
at the same seam for the same reasons.
"""

from typing import Any, Optional

from .errors import InvalidPriorityError

# AMQP carries priority as one octet, so 255 is the ceiling in both places.
# RabbitMQ additionally builds internal structures per priority level, so a
# large x-max-priority costs real memory and throughput even when it is legal;
# see Config.RECOMMENDED_MAX_PRIORITY for the value to actually use.
MIN_QUEUE_MAX_PRIORITY = 1
MAX_QUEUE_MAX_PRIORITY = 255
MIN_MESSAGE_PRIORITY = 0
MAX_MESSAGE_PRIORITY = 255


def _require_int(value: Any, label: str) -> int:
    """
    Reject anything that is not a true integer.

    ``bool`` is excluded explicitly because ``isinstance(True, int)`` is True
    in Python, and ``max_priority=True`` silently meaning 1 is exactly the
    class of surprise this module exists to prevent.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPriorityError(
            f"{label} must be an integer, got {value!r} ({type(value).__name__}). "
            "Note that a float is not silently truncated here: aio-pika would "
            "have stored 1.5 as 1 with no error."
        )
    return value


def validate_max_priority(value: Optional[int]) -> Optional[int]:
    """
    Validate a queue's ``x-max-priority``.

    ``None`` means the queue is declared exactly as it was before priority
    support existed — no ``x-max-priority`` argument at all. That is the
    backward-compatibility default and it is deliberately preserved here
    rather than coerced to a number.
    """
    if value is None:
        return None

    _require_int(value, "max_priority")

    if not (MIN_QUEUE_MAX_PRIORITY <= value <= MAX_QUEUE_MAX_PRIORITY):
        raise InvalidPriorityError(
            f"max_priority must be between {MIN_QUEUE_MAX_PRIORITY} and "
            f"{MAX_QUEUE_MAX_PRIORITY}, got {value}. RabbitMQ rejects anything "
            "outside that range with a 406 that closes the channel. Values "
            "above 10 are legal but wasteful — see Config.RECOMMENDED_MAX_PRIORITY."
        )
    return value


def validate_message_priority(value: Optional[int]) -> Optional[int]:
    """
    Validate a single message's ``priority``.

    ``None`` means "do not set one", which the broker treats identically to 0
    (verified: on a priority queue an unset message and a priority-0 message
    sort the same and stay FIFO relative to each other).
    """
    if value is None:
        return None

    _require_int(value, "priority")

    if not (MIN_MESSAGE_PRIORITY <= value <= MAX_MESSAGE_PRIORITY):
        raise InvalidPriorityError(
            f"priority must be between {MIN_MESSAGE_PRIORITY} and "
            f"{MAX_MESSAGE_PRIORITY}, got {value}."
        )
    return value
