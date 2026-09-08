# Testing

> Three levels of test for a protobus service, what each one is actually worth, and the isolation rules that stop them fighting each other.

**Read this if** you are writing tests for a service you built with protobus. Testing protobus itself is a different job; this page is about your code.

| | |
|---|---|
| **Prerequisites** | [Getting Started](./getting-started.md) — you have a service and a proxy that calls it |
| **Next** | [Error Handling](./error-handling.md) · [Architecture](../concepts/architecture.md) |
| **Source** | [`pyproject.toml`](../../pyproject.toml) (`[tool.pytest.ini_options]`) · [`tests/integration/conftest.py`](../../tests/integration/conftest.py) · [`docker-compose.yml`](../../docker-compose.yml) · [`scripts/run-combat-sample.sh`](../../scripts/run-combat-sample.sh) |

**On this page** — [Three levels](#three-levels) · [Level 1: the handler alone](#level-1-the-handler-alone) · [Level 2: against a real broker](#level-2-against-a-real-broker) · [Isolating tests from each other](#isolating-tests-from-each-other) · [Asserting on events](#asserting-on-events) · [Asserting on failures](#asserting-on-failures) · [Level 3: end-to-end](#level-3-end-to-end) · [Testing your documentation](#testing-your-documentation)

---

## Three levels

| Level | Needs a broker | Cost per test | What it can catch |
|---|---|---|---|
| **1. Handler alone** | no | ~1 ms | your business logic, validation, error classification |
| **2. Service + broker** | yes | ~100 ms–1 s | encoding, routing, retries, events, timeouts |
| **3. End-to-end script** | yes | seconds | wiring, shutdown, "the whole thing runs" |

Most service suites should be mostly level 1, and most are not — people reach for a broker because the service class *looks* like it needs one. It does not. Start at level 1 and go up only when the thing you want to assert genuinely lives in the transport.

---

## Level 1: the handler alone

A protobus handler is a normal coroutine method. `MessageService` calls it as `handler(data, actor, correlation_id[, context])` ([`protobus/message_service.py`](../../protobus/message_service.py), `_on_message`) — the request dict first, then metadata. There is nothing magic to reproduce.

The one thing the constructor does need is a context object whose `connection` can register a listener: `MessageService` builds its listeners eagerly, and each one attaches a reconnection restorer via `attach_restorer`, which falls back to `connection.on('reconnected', …)` when the connection has no `register_restorer` ([`protobus/connection.py`](../../protobus/connection.py)). Two no-op methods satisfy it.

```python
# orders_service.py
from types import SimpleNamespace

from protobus import HandledError, MessageService


class _StubConnection:
    """Enough of a connection to construct a service. Never init() against this."""

    def on(self, *args, **kwargs): ...
    def remove_listener(self, *args, **kwargs): ...


def stub_context():
    return SimpleNamespace(connection=_StubConnection(), factory=None)


class OrdersService(MessageService):
    service_name = "Orders.Service"
    proto_file_name = "Orders.proto"

    async def create(self, request: dict, actor: str, correlation_id: str) -> dict:
        if not request.get("customerId"):
            raise HandledError("customerId is required", "VALIDATION_ERROR")
        return {"id": f"order-{request['customerId']}", "cents": request.get("cents", 0)}
```

The stub is deliberately thin, and worth being honest about: it says *this object is only ever used to construct, never to connect*, which is exactly the contract of a level-1 test. (This repository's own unit suite uses a fuller `FakeContext` / `FakeConnection` pair in [`tests/helpers.py`](../../tests/helpers.py), because it tests the framework's own plumbing; your service does not need that.)

```python
# test_orders_service.py
import pytest

from orders_service import OrdersService, stub_context

service = OrdersService(stub_context())


async def test_returns_an_order_id():
    assert await service.create({"customerId": "c1", "cents": 500}, "", "") == {"id": "order-c1", "cents": 500}


async def test_rejects_a_missing_customer_id_as_terminal_not_retriable():
    with pytest.raises(HandledError) as info:
        await service.create({}, "", "")
    assert info.value.code == "VALIDATION_ERROR"
    assert info.value.is_handled is True
```

No `init()`, no `await context.init(...)`, no broker, no queues to clean up. Asserting `is_handled is True` is worth doing explicitly — it is the difference between a caller getting an answer in milliseconds and a caller waiting out the retry ladder, and it is invisible in the happy path.

> [!TIP]
> If a handler is hard to test this way it is usually because it reaches for I/O directly. Take the dependency as a constructor argument and the level-1 test becomes trivial — which is ordinary advice, but protobus makes it cheap to ignore because the service class is easy to construct.

The tests above are plain `async def` functions because this repository sets `asyncio_mode = "auto"` for [pytest-asyncio](https://pypi.org/project/pytest-asyncio/) in `pyproject.toml`. Without that setting, mark them `@pytest.mark.asyncio`.

---

## Level 2: against a real broker

There is no in-memory transport, and no mock worth building: the behaviour you are testing at this level *is* RabbitMQ's. Use a real one in Docker.

This repo's own compose file is the whole setup ([`docker-compose.yml`](../../docker-compose.yml)):

```yaml
services:
  rabbitmq:
    image: rabbitmq:3-management
    container_name: protobus-rabbitmq
    ports:
      - "5672:5672"     # AMQP
      - "15672:15672"   # management UI, guest / guest
    environment:
      RABBITMQ_DEFAULT_USER: guest
      RABBITMQ_DEFAULT_PASS: guest
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 30s
      timeout: 10s
      retries: 5
```

The healthcheck is the part that matters, because it is what makes `--wait` mean anything:

```bash
docker compose up -d --wait     # blocks until the healthcheck passes
python -m pytest tests/integration
docker compose down
```

> [!WARNING]
> `docker compose up -d` without `--wait` returns as soon as the container starts, several seconds before RabbitMQ accepts connections. The result is a suite that passes locally and fails on the first run in CI, which is the least useful failure mode there is.

### Two suites, not one

Unit tests and broker tests want different settings, so they are kept apart.

| | `tests/unit` | `tests/integration` |
|---|---|---|
| in `testpaths` (runs by default) | **yes** | no — name it explicitly |
| broker | none — `FakeConnection` | real; **skipped**, not failed, when nothing answers at `PROTOBUS_TEST_AMQP_URL` |
| marker | — | `integration`, added by the conftest |
| timeout | `pytest-timeout`, `--timeout 180` in CI | same |

`python -m pytest` therefore runs only the unit tests, and the broker suite is opt-in. Copy that split; a suite that silently needs Docker is a suite people stop running. The skip-when-unreachable check in [`tests/integration/conftest.py`](../../tests/integration/conftest.py) is a plain socket connect with a one-second timeout — cheap enough to run on every collection.

---

## Isolating tests from each other

This is where broker tests actually go wrong, and it has one cause: **a protobus service's queues are durable, named after the service, and their arguments are fixed at declare time.**

Two consequences:

1. Two test modules that both declare `Orders.Service` compete for the same queue. Run serially they merely interfere; under `pytest-xdist`, one module's service consumes the other module's messages and both fail confusingly.
2. Changing `retry_delay_ms` between runs fails startup. It becomes the retry queue's `x-message-ttl`, RabbitMQ cannot change that in place, and protobus surfaces the broker's `PRECONDITION_FAILED` as a [`RetryQueueMismatchError`](../reference/errors.md#retryqueuemismatcherror) telling you to drain and delete the queue.

The fix this repo uses is to stamp the service name per run. From [`tests/integration/conftest.py`](../../tests/integration/conftest.py):

```python
RUN_ID = uuid.uuid4().hex[:8]


def unique(name: str) -> str:
    """A service/package name unique to this test run."""
    return f"{name}{RUN_ID}"
```

The stamp becomes the proto package, so every queue name is unique to the run, and the schema is handed to the service as text rather than read from disk:

```python
from protobus import Context, MessageService, MessageServiceOptions, RetryOptions


def proto_for(pkg: str) -> str:
    return f'''syntax = "proto3";
package {pkg};

message Request  {{ string tag = 1; }}
message Response {{ string tag = 1; }}

service Service {{
    rpc handle({pkg}.Request) returns({pkg}.Response);
}}'''


def recording_service(pkg: str):
    class RecordingService(MessageService):
        proto_file_name = ""            # nothing on disk: Proto below is the schema
        service_name = f"{pkg}.Service"
        Proto = proto_for(pkg)

        def __init__(self, context: Context):
            super().__init__(context, MessageServiceOptions(max_concurrent=1, retry=RetryOptions(max_retries=0)))
            self.handled: list[str] = []

        async def handle(self, request: dict, actor: str, correlation_id: str) -> dict:
            self.handled.append(request["tag"])
            return {"tag": request["tag"]}

    return RecordingService
```

Then delete the queues afterwards, with a plain `aiormq` connection — protobus has no delete API, and this is what the repo's `cleanup_queues` fixture does:

```python
import aiormq
import pytest


@pytest.fixture
async def cleanup_queues(amqp_url):
    """Collects queue names; deletes them (best effort) after the test."""
    names: list[str] = []
    yield names
    if not names:
        return
    conn = await aiormq.connect(amqp_url)
    ch = await conn.channel()
    for name in names:
        try:
            await ch.queue_delete(name)
        except Exception:
            pass  # already gone
    await conn.close()
```

> [!IMPORTANT]
> **How many queues to delete depends on `max_retries`.** With `RetryOptions(max_retries=0)` the listener returns before declaring the retry and DLQ queues at all ([`protobus/message_listener.py`](../../protobus/message_listener.py), `setup_retry_queues`), so there are two: `<Service>` and `<Service>.Events`. With retries enabled there are four — add `<Service>.Retry` and `<Service>.DLQ`. Setting `max_retries=0` in tests that are not *about* retries is worth doing for that reason alone, and because it removes multi-second delays from every failure assertion.

The callback queue needs no cleanup: it is exclusive and auto-delete, and disappears with the client process.

### Close in the fixture's teardown

```python
@pytest.fixture
async def stack(amqp_url, cleanup_queues):
    context = Context()
    await context.init(amqp_url, [])
    service = recording_service(unique("Orders"))(context)
    await service.init()
    cleanup_queues.extend([service.service_name, f"{service.service_name}.Events"])
    proxy = ServiceProxy(context, service.service_name)
    await proxy.init()
    yield service, proxy
    await service.close()
    await context.close()
```

Close first, then delete: deleting a queue that still has a consumer works, but leaves the consumer's channel erroring on the way out. The `cleanup_queues` fixture is requested *before* the stack, so pytest tears it down *after* — fixture teardown runs in reverse order of setup.

---

## Asserting on events

An event is fire-and-forget, so a test has to wait for it rather than await it. The pattern in [`tests/integration/test_message_service.py`](../../tests/integration/test_message_service.py) is a future the subscriber resolves:

```python
async def test_publishes_order_created_when_an_order_is_created(stack):
    service, proxy = stack
    received: asyncio.Future = asyncio.get_running_loop().create_future()

    async def on_created(event: dict) -> None:
        if not received.done():
            received.set_result(event)

    await service.subscribe_event("Orders.OrderCreated", on_created)

    await proxy.create({"customerId": "c1"})

    event = await asyncio.wait_for(received, 2)
    assert event["customerId"] == "c1"
```

Two things to get right:

- **Subscribe before you publish.** The events queue is durable and not auto-delete, so a message published first is not lost — but the binding is only added by `subscribe_event`, and a message published before the binding exists routes nowhere. Await the subscription, then act.
- **Give it a deadline.** A bare `await received` under `pytest-timeout` fails after the whole budget with "Timeout >180s", which does not say which side broke. `asyncio.wait_for(received, 2)` produces a far better failure message.

Wildcard topics work the same way — `subscribe_event(type, handler, "CUSTOM.*.TOPIC")` — and the matching rules are the trie's, documented and pinned in [`tests/unit/test_events_trie_config.py`](../../tests/unit/test_events_trie_config.py) (`test_the_wildcard_example_in_the_docs`). `*` is exactly one word; `#` is zero or more. `ORDERS.*.CREATED` does **not** match `ORDERS.US.123.CREATED`.

---

## Asserting on failures

The assertion that matters is not "it raised" — it is **how many times the handler ran**. That is the only way to see the retry classification, and it is invisible from the caller's side.

```python
async def test_answers_a_handled_error_immediately_and_does_not_retry(stack):
    service, proxy = stack
    service.calls.clear()

    with pytest.raises(RemoteError) as info:
        await proxy.create({})
    assert info.value.code == "VALIDATION_ERROR"

    await asyncio.sleep(0.5)          # a retry would land inside this window
    assert len(service.calls) == 1
```

The `sleep` is load-bearing. Without it the test passes even if the message *is* being retried, because the error arrives long before the second attempt does.

| Raised | Handler invocations | Caller waits |
|---|---|---|
| `HandledError` (or anything with `is_handled = True`) | 1 | one handler run |
| plain `Exception` | 1 + `max_retries` = **4** at the defaults | ~`max_retries × retry_delay_ms` = **~15 s** |

Both numbers are asserted in [`tests/integration/test_dispatcher_and_retry.py`](../../tests/integration/test_dispatcher_and_retry.py): the handled case in `test_does_not_retry_when_a_handled_error_is_raised`, the exhausted case in `test_sends_to_the_dlq_after_max_retries_are_exceeded` with its "initial + 3 retries" comment.

> [!CAUTION]
> A test that raises a plain exception at the default `retry_delay_ms` needs a budget above the ladder: ~15 s of parking, prefetch 1, and a single message queued ahead of it pushes the total further. The repo's own retry suite uses `retry_delay_ms=100` for exactly this reason. If your failure test is flaky on a loaded machine, this is why — set `RetryOptions(max_retries=0)` unless retrying is the thing under test, or shorten the delay.

Errors the caller raises locally — `RpcTimeoutError`, the publish failures, `NotReadyError` — never reach a handler at all and are matched on class. See [Errors](../reference/errors.md#which-error-am-i-looking-at) for the full table.

---

## Level 3: end-to-end

Above the integration suite there is one more test worth having: **run the real thing and assert on what it printed.**

[`scripts/run-combat-sample.sh`](../../scripts/run-combat-sample.sh) is that test for protobus itself. It runs `sample/combatGame` against a live broker and greps the output:

```bash
docker compose up -d --wait
PYTHON=$PWD/venv/bin/python scripts/run-combat-sample.sh
```

```
==> Result: <n> shots fired, 1 winner(s)
PASS: combat game completed with exactly one winner
```

The shot count varies per run; the winner count does not. The assertions are three: a non-zero exit fails, `(WINNER!)` must appear exactly once, and at least one `shoots at` must appear — that last one because a run that fires no shots exits cleanly and proves nothing. The header comment states the case for it plainly: this is the only exercise of the framework as a consumer sees it — proto loading from disk, instance-named services, RPC, pub/sub events and shutdown in one process — so a regression the unit and integration suites miss shows up here as "no winner" or "several winners".

One mechanic in that script is worth copying if you write your own: it runs the sample as a module (`python -m sample.combatGame.game_runner`) from the repository root, so the sample's `.proto` is found relative to its own `__file__` and the library is imported from the checkout. **A protobuf schema is an asset your packaging does not move for you** — put `*.proto` in your package data, or ship it beside the code that loads it.

### `sample/combatGame` is the worked example

Six player services, each a `MessageService` with its own strategy, all in one process: RPC between players (`shoot`), pub/sub for the six event types they each subscribe to, and a disconnect at the end. It is the most complete example in the repo, and the only one that exercises RPC, events and shutdown together — read it before writing your own end-to-end test rather than after.

| | |
|---|---|
| Entry point | [`sample/combatGame/game_runner.py`](../../sample/combatGame/game_runner.py) |
| Schema | [`sample/combatGame/player.proto`](../../sample/combatGame/player.proto) |
| Services | [`sample/combatGame/players/`](../../sample/combatGame/players) — six strategies over one `BasePlayer` |
| Run it | `PYTHON=$PWD/venv/bin/python scripts/run-combat-sample.sh` |

For streaming, [`sample/tokenStream`](../../sample/tokenStream) is the equivalent: a server-streaming RPC with a client that consumes it and cancels it.

> [!TIP]
> Leave the management UI open at <http://localhost:15672> (`guest`/`guest`) while a broker test runs. Queue depth, consumer count and unacked messages answer most "why did that hang" questions in about five seconds, and none of them are visible from inside the test.

---

## Testing your documentation

Code in a README rots silently, and a reader trusts it more than they trust the source. This repo runs its own tutorial: [`scripts/check-getting-started.sh`](../../scripts/check-getting-started.sh) writes every file from [Getting Started](./getting-started.md) into a scratch project, starts the server and the subscriber against a live broker, runs the client, and asserts on the exact lines the page says each process prints.

```bash
docker compose up -d --wait
PYTHON=$PWD/venv/bin/python scripts/check-getting-started.sh
```

Claims a snippet cannot assert about itself — which wildcard matches which topic, what a zero value decodes to, what an operator reads off a DLQ message — are pinned in the unit and integration suites, and the page that makes the claim names the test next to it. The idea transfers to any repository whose docs contain code, and it is perhaps a hundred lines of work.

---

<div align="center">

**[← Message Priority](./priority.md)** · **[Docs index](../README.md)** · **[Architecture →](../concepts/architecture.md)**

</div>
