# Changelog

All notable changes to **protobus-py** are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] — 2026-09-01

### Added

- **Message priority.** A service can opt its queue into RabbitMQ priority
  ordering so control traffic jumps a bulk backlog sitting on the same queue —
  the case where a control RPC fans out thousands of messages onto its own
  queue and the next control RPC then waits behind all of them. One queue, no
  extra service. See [`docs/advanced/message-priority.md`](docs/advanced/message-priority.md).
  - `MessageServiceOptions(max_priority=...)` → the queue's `x-max-priority`.
    Also on `MessageListener` and `BaseListener` as a `max_priority` argument.
  - Keyword-only `priority` on every `ServiceProxy` unary method, threaded
    through `Context.publish_message` and `MessageDispatcher.publish`, and
    available as a `priority` key in `Connection.publish`'s `properties`.
  - `Config.PRIORITY_NORMAL` (0), `Config.PRIORITY_HIGH` (1),
    `Config.PRIORITY_CONTROL` (2), `Config.RECOMMENDED_MAX_PRIORITY` (2).
    Three rungs deliberately: RabbitMQ builds internal structures per priority
    level, so a large ceiling costs real memory and throughput.
  - New error `InvalidPriorityError` (a `ValueError`) and the validators
    `validate_max_priority` / `validate_message_priority`.

### Notes

- **This is opt-in and backward compatible in both directions.** A listener
  that does not pass `max_priority` declares its queue with the identical
  argument set as before — no `x-max-priority` key at all. Publishing a
  priority to a queue that has no `x-max-priority` is accepted and ignored by
  the broker, so a new client is safe against an old service, and an old
  client is safe against a new one.

- ⚠️ **Enabling priority on a queue that already exists needs a one-time
  operator migration: drain, delete, recreate.** RabbitMQ answers a re-declare
  that adds `x-max-priority` with a `406 PRECONDITION_FAILED`, which closes the
  channel it was made on. Other listeners on the same connection survive that
  (verified), but the declare happens inside `init()`, so the listener never
  starts and `MessageService.init()` raises — the service fails to boot.
  Re-declaring with the *same* `x-max-priority` is idempotent, so restarts
  after the migration are fine.
  Only the service queue needs it; `.retry` and `.DLQ` are deliberately left
  alone, because a message keeps its priority across a dead-letter hop.

- **Priority reorders the queue, not the consumers' prefetch buffers.** With
  prefetch `N` across `R` replicas, up to `N × R` messages can still sit ahead
  of a high-priority one. It shrinks the window by orders of magnitude; it does
  not eliminate it. Lower the prefetch if you need a tighter bound.

- **Port parity.** The TypeScript port gains the same feature in the same
  release, with the same concepts and wire behaviour (`maxPriority` /
  `priority`, the same three constants, the same validation range). One
  harmless representational difference: amqplib omits the AMQP `priority`
  property when unset, whereas aio-pika normalizes it to `0` — as it always
  has, before this change. RabbitMQ treats absent and `0` identically, which is
  pinned by a real-broker test.

- Validation exists because neither underlying failure is usable as-is:
  aio-pika applies `int()` to a priority, so `1.5` would be silently stored as
  `1`; an out-of-range value surfaces as a raw `struct.error` from the encoder;
  and an out-of-range `x-max-priority` is a channel-killing 406.

- Tests: 106/106 pass (55 existing + 51 priority). The broker-backed ones need
  a live RabbitMQ (`docker-compose up -d`).

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
