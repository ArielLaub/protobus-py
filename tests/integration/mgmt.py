"""RabbitMQ management API helpers for tests that need a broker-side action:
a fresh vhost, a severed connection, a queue's argument list."""

import asyncio
import urllib.parse
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

MGMT = os.environ.get("RABBITMQ_MGMT", "http://guest:guest@localhost:15672")
BASE = urllib.parse.urlsplit(MGMT)
ORIGIN = f"{BASE.scheme}://{BASE.hostname}:{BASE.port or 15672}"
AUTH = "Basic " + base64.b64encode(f"{BASE.username}:{BASE.password}".encode()).decode()


def api(path, method="GET", body=None):
    request = urllib.request.Request(f"{ORIGIN}{path}", method=method, headers={"Authorization": AUTH, "Content-Type": "application/json"})
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(request, data=data, timeout=5) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def management_available():
    try:
        api("/api/overview")
        return True
    except Exception:
        return False


async def run_blocking(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


async def create_vhost(name, amqp_url):
    """Create an empty vhost the test user can use; returns its AMQP URL."""
    encoded = urllib.parse.quote(name, safe="")
    await run_blocking(api, f"/api/vhosts/{encoded}", "PUT")
    await run_blocking(api, f"/api/permissions/{encoded}/{urllib.parse.quote(BASE.username, safe='')}", "PUT", {"configure": ".*", "write": ".*", "read": ".*"})
    parts = urllib.parse.urlsplit(amqp_url)
    return f"amqp://{BASE.username}:{BASE.password}@{parts.hostname}:{parts.port or 5672}/{encoded}"


async def delete_vhost(name):
    try:
        await run_blocking(api, f"/api/vhosts/{urllib.parse.quote(name, safe='')}", "DELETE")
    except Exception:
        pass


def connections_in(vhost):
    return [c for c in api("/api/connections") if c.get("vhost") == vhost]


async def wait_for_connections(vhost, minimum=1, timeout=30):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        mine = await run_blocking(connections_in, vhost)
        if len(mine) >= minimum or loop.time() > deadline:
            return mine
        await asyncio.sleep(0.5)


async def kill_connections(vhost):
    mine = await wait_for_connections(vhost)
    for conn in mine:
        await run_blocking(api, f"/api/connections/{urllib.parse.quote(conn['name'], safe='')}", "DELETE")
    return len(mine)
