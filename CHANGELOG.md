# Changelog

All notable changes to **protobus-py** are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] — 2026-09-01

Parity pass against the two audits already merged into the TypeScript port
(`ArielLaub/protobus` v1.5.0, v2.0.0 and PR #24 / audit-2.0.1), plus the
Python-only defects those audits' questions turned up. Scope was deliberately
limited to **silent data corruption or loss** and **hangs and leaks**;
cross-port divergences and lower-severity items are listed as known debt in
the pull request rather than half-fixed here.

### Fixed — message loss

- **Every retry destroyed its message.** `_retry_message` built its message with
  `expiration=str(retry_delay_ms)`, and aio-pika's `encode_expiration` is a
  `singledispatch` registered for `int`, `float`, `timedelta` and `datetime` —
  not `str`. It raised `ValueError` at publish time on every aio-pika ≥ 9; the
  surrounding bare `except` logged it, and the delivery was acked regardless.
  Any message whose handler raised a non-`HandledError` was destroyed on its
  first retry, on every install. `expiration` is now a number of seconds.
- **The retry publish was addressed to nothing.** It published to the topic bus
  exchange with the key `"<delivery routing key>.retry"` — four segments
  against the three-segment binding a service declares — while the retry queue
  is named after the *consumer's queue* and is bound to nothing. Verified
  against a real broker: the broker returns the message as unroutable. Retries
  now go to the retry queue by name through the default exchange, the technique
  `_send_to_dlq` already used.
- **An unroutable publish was dropped in silence.** aio-pika sends `mandatory`
  but reports the broker's `basic.return` in the *return value*, not by
  raising: a routed publish resolves to `Basic.Ack`, an unroutable one to a
  `DeliveredMessage` wrapping `Basic.Return`. Nothing here ever looked. This is
  what made a request to a service with no consumers look like a timeout rather
  than an error. Requests now raise `UnroutableError`; events stay
  non-mandatory, because having no subscribers is normal for an event.
- **A failed retry/DLQ handoff still acked the delivery.** The handoff is now
  confirmed before the ack, and a failure requeues instead of dropping.
- **A second subscriber to a topic silently replaced the first.** Each trie node
  held one `value` slot and registration assigned into it, so only the last
  subscriber was ever called while the binding stayed in place and the broker
  kept delivering. Nodes now hold a list.

### Fixed — silent data corruption

- **`bigint` reshaped values it could not represent.** A negative was
  two's-complemented into 256 bits and read back by the unsigned decoder as a
  vast positive (`-5` round-tripped to `2**256 - 5`); anything wider than 32
  bytes was truncated to its low 32 (`2**256 + 7` round-tripped to `7`). Both
  now raise `ValueError`, matching the TS port's `RangeError`. Decoding is
  bounded at the 32-byte wire width.
- **An undecodable payload was handed to the handler as `{}`.** The
  protobuf→JSON→`return {}` ladder made a payload the service could not read
  indistinguishable from a genuinely empty one. It now raises `ProtocolError`.
- **An unencodable payload was silently downgraded to JSON.** When a protobuf
  type *is* declared, falling back to `json.dumps` puts JSON bytes into a field
  the peer decodes against that type. It now raises `InvalidRequestError`.
  Unknown fields are ignored rather than rejected, matching protobufjs.
- **An error reply with no method decoded as a success.** `decode_response`
  required a non-empty `error.method`, so an error raised before the method was
  known — the reply to a request that did not decode — fell through to the
  result path and reached the caller as an empty successful result.
- **A truncated stream looked like a finished one.** Losing the connection
  mid-stream pushed the same `_STREAM_END` sentinel a *complete* stream ends
  with, so the caller's `async for` ended normally and the prefix was treated
  as the whole answer. (The docstring claimed `StreamTimeoutError` would be
  raised; it never was.) It now raises `DisconnectedError`.
- **Streaming sequence gaps were never checked.** `x-protobus-seq` was written
  and never read, so a lost chunk was delivered as a shorter but apparently
  complete stream. Gaps now raise `StreamSequenceError`.

### Fixed — security

- **The message body chose the method.** The handler was resolved as
  `getattr(self, request.method.split(".")[-1])` with nothing checked against
  the `.proto`, the owning service, or the routing key. `getattr` walks the
  whole MRO, so a publisher holding no more than the ordinary right to call the
  service could name `init`, `publish_event`, `cleanup` or `_on_message` and
  reach them; append a segment to redirect dispatch; or name another service's
  method to have the payload parsed under a foreign schema. Dispatch is now
  bound to the service's own contract: the body's method must name this
  service, must agree with the routing key RabbitMQ authorised, must be
  declared in the `.proto` when one is loaded, and must resolve to a member the
  subclass itself implements — the walk stops at `MessageService`.
- Connection URLs are redacted before they reach a log.

### Fixed — hangs

- **A unary RPC used the server's handler budget as the caller's deadline.**
  `MESSAGE_PROCESSING_TIMEOUT` defaults to 600 000 ms, so a call to a service
  that was scaled to zero blocked its caller for ten minutes. Added
  `RPC_CALL_TIMEOUT_MS` (default 30 000 ms) and `RpcTimeoutError`.
- **An undecodable request was retried instead of answered.** `decode_request`
  ran outside any `try`, so a body that did not parse raised a plain exception:
  `is_handled_error()` said `False` and the delivery went through the full
  retry ladder and a DLQ publish while the caller waited out its timeout for a
  reply the retries were never going to produce. It is now answered at once.
  `ProtocolError` and `InvalidMethodError` are `HandledError`s.
- **No AMQP heartbeat was negotiated**, so the interval was whatever RabbitMQ
  proposed — 60 s — and a peer that vanished without closing its socket went
  unnoticed for around two minutes while the connection reported itself
  healthy. `AMQP_HEARTBEAT_SECONDS` defaults to 30. A heartbeat already in the
  URL is left alone, `heartbeat=0` included; the rest of the URL is preserved
  byte for byte so a percent-encoded vhost is not re-encoded.
- **The streaming chunk buffer was unbounded.** `StreamBackpressureError` had
  existed since streaming shipped without ever being raised. Bounded by
  `STREAM_MAX_BUFFERED_CHUNKS` (default 256).
- **A broken `.proto` came up looking healthy.** `RunnableService.start` wrapped
  schema registration in a bare `except Exception` and registered an empty
  schema on any failure. Only a genuinely missing `.proto` — the JSON-mode case
  — is tolerated now.

### Added

- Errors: `ProtocolError`, `RpcTimeoutError`, `PublishError`, `UnroutableError`,
  `StreamSequenceError`.
- Config: `RPC_CALL_TIMEOUT_MS`, `AMQP_HEARTBEAT_SECONDS`,
  `STREAM_MAX_BUFFERED_CHUNKS`.
- `Connection.publish()` takes `mandatory` (default `True`).

### Changed

- `MessageHandler` may declare an optional fourth positional parameter and
  receive the routing key the delivery arrived on. Handlers that do not declare
  it are called exactly as before — the arity is resolved once per consumer.
- `InvalidMethodError` is now a `HandledError`, so an unknown method is
  answered rather than retried.
- Tests read their broker URL from `PROTOBUS_TEST_AMQP_URL`. Hardcoding
  `localhost:5672` is a live hazard: anything else bound to loopback 5672 — a
  `kubectl port-forward` to a cluster broker is the usual one — silently takes
  precedence over the local docker-compose container, and the suite then
  declares queues and publishes on shared infrastructure while reporting a
  clean pass.

### Notes

- Tests: 135 pass (55 pre-existing + 80 new), against a real RabbitMQ. Every
  new fix was mutation-checked: the fix was reverted one at a time and the test
  naming it confirmed to fail, 18/18. Two of those reverts stayed green on the
  first attempt and exposed real gaps — the dispatch boundary had to move from
  MessageService to the whole protobus package, and several refusal assertions
  were being satisfied by an incidental TypeError rather than by the guard.
- Routing, publisher confirms, `basic.return` and heartbeat negotiation are
  verified against a real broker in `tests/test_audit_broker.py`; they cannot
  be established with mocks.

## [1.4.0] — 2026-06-04

### Added

- **Server-streaming RPC.** Methods declared `rpc foo (Req) returns (stream Chunk)`
  in `.proto` return an `AsyncIterator[Chunk]` on the client (consumed with
  `async for`) and accept an async-generator handler on the server. End-of-stream
  is signaled via the `x-protobus-final` AMQP header — no `ResponseContainer`
  schema change. See [`docs/advanced/streaming.md`](docs/advanced/streaming.md).
- **Cross-language compatibility.** A TS client (`protobus@1.4.0`) drives a
  Python streaming server identically to a Python client. The chat-path
  topology (API in TS → ChatAgent in Python) is the motivating case and is
  verified by `protobus`'s `test/integration/cross-language.test.ts`.
- New errors: `StreamingError`, `StreamTimeoutError`, `StreamBackpressureError`,
  `StreamClosedError`.
- New config: `STREAM_IDLE_TIMEOUT_MS` env var (default `60000` ms) — idle
  timeout between streaming chunks. Streaming calls do NOT use
  `MESSAGE_PROCESSING_TIMEOUT` — a stream may legitimately take far longer
  than any single chunk gap.
- New public API: `Context.publish_streaming_message()`,
  `MessageDispatcher.publish_streaming()`, `MessageFactory.is_streaming_method()`.

### Fixed

- **Version drift between `pyproject.toml` and `protobus/__init__.py`.**
  These had silently disagreed (1.3.2 vs 1.2.1) since an earlier release.
  Both are now aligned at 1.4.0; release scripts should keep them in lockstep.

### Changed

- `MessageHandler` type alias gained an optional `headers` parameter:
  `Callable[[bytes, str, dict], Awaitable[Any]]`. Existing handlers with the
  old 2-arg signature keep working — `headers` has a default value where the
  framework calls handlers.
- Handlers may now return an `AsyncIterator[bytes]` (in addition to
  `Optional[bytes]`) to drive a streaming reply.

### Notes

- Tests: 55/55 pass (44 existing + 11 streaming).
- Requires `protoc` on PATH for descriptor-pool loading (unchanged from prior
  releases). The Homebrew package `protobuf` provides this on macOS.
