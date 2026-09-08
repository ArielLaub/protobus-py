"""
Where the integration tests find a broker.

Hardcoding ``amqp://guest:guest@localhost:5672/`` is a live hazard on a
developer machine: anything else bound to loopback 5672 — a ``kubectl
port-forward`` to a cluster broker is the usual one — silently takes
precedence over the local docker-compose container, and the suite then
declares its queues, publishes and consumes on shared infrastructure while
reporting a clean pass. Nothing in the output distinguishes the two.

Set PROTOBUS_TEST_AMQP_URL to point the suite somewhere specific.
"""

import os

DEFAULT_URL = "amqp://guest:guest@localhost:5672/"


def broker_url() -> str:
    """The AMQP URL the integration tests should use."""
    return os.environ.get("PROTOBUS_TEST_AMQP_URL", DEFAULT_URL)
