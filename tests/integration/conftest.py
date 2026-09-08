"""
Integration tests run against a real RabbitMQ.

They are skipped, not failed, when no broker answers at the configured URL
(``PROTOBUS_TEST_AMQP_URL``, default ``amqp://guest:guest@localhost:5672/``).
Queue names carry a per-run suffix so a queue argument that changed between
runs (a different retry delay, a priority ceiling) cannot 406 against the
previous run's leftovers, and every queue a test declares is deleted after it.
"""

import asyncio
import os
import socket
import uuid
from typing import List
from urllib.parse import urlsplit

import pytest

from .broker_url import broker_url

RUN_ID = uuid.uuid4().hex[:8]


def unique(name: str) -> str:
    """A service/package name unique to this test run."""
    return f"{name}{RUN_ID}"


def _broker_reachable(url: str) -> bool:
    parts = urlsplit(url)
    host, port = parts.hostname or "localhost", parts.port or 5672
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def amqp_url() -> str:
    url = broker_url()
    if not _broker_reachable(url):
        pytest.skip(f"no RabbitMQ at {url} (set PROTOBUS_TEST_AMQP_URL)")
    return url


@pytest.fixture
async def cleanup_queues(amqp_url):
    """Collects queue names; deletes them (best effort) after the test."""
    names: List[str] = []
    yield names
    if not names:
        return
    import aiormq

    try:
        conn = await aiormq.connect(amqp_url)
        ch = await conn.channel()
        for name in names:
            try:
                await ch.queue_delete(name)
            except Exception:
                pass
        await conn.close()
    except Exception:
        pass


@pytest.fixture
async def fresh_vhost(amqp_url):
    """An empty vhost of its own for this test: nothing declared, no leftovers
    from a previous run or a previous test. Skipped without the management
    plugin. Yields the vhost's AMQP URL."""
    from . import mgmt

    if not await mgmt.run_blocking(mgmt.management_available):
        pytest.skip(f"RabbitMQ management API not reachable at {mgmt.ORIGIN}")
    name = unique("protobus-fresh-")
    url = await mgmt.create_vhost(name, amqp_url)
    yield url
    await mgmt.delete_vhost(name)


def pytest_collection_modifyitems(items):
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
