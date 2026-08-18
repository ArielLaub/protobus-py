# Changelog

All notable changes to **protobus-py** are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.1] — 2026-08-19

### Fixed

- **Reconnection leaked AMQP resources without bound.** Every `reconnected`
  event made each listener and dispatcher open a *fresh* channel and drop the
  previous one without closing it, `MessageDispatcher` additionally built a
  brand-new `CallbackListener` (channel + exclusive queue + consumer) per
  reconnect, and nothing ever unregistered a component's connection event
  handlers — so discarded components kept opening channels forever and the
  growth compounded to O(N²) in the number of reconnects. On top of that,
  `Connection._reconnect()` replaced the underlying connection without closing
  the old one or detaching from it; since aio-pika's robust connections
  re-establish themselves, each abandoned connection came back with its
  channels and consumers attached and could start yet another reconnect loop.
  Two Python services drove a shared broker to repeated OOMKills this way
  (199 connections / 30,037 channels, climbing ~260 channels/sec, while the
  broker was flapping). Eight forced disconnects of a single process now hold
  steady at 2 connections / 6 channels / 4 consumers; before this fix the same
  eight reached 100 / 3,002 / 775 and were still climbing.
- **An anonymous queue could not be restored after a reconnect.** Re-setup
  re-declared the queue by its previous broker-generated `amq.gen-*` name,
  which the broker refuses (reserved prefix). Anonymous listeners now always
  ask for a fresh queue, named listeners keep their configured name, and
  direct-exchange listeners re-derive their self-binding — so a callback queue
  survives a reconnect instead of only working because the dispatcher happened
  to rebuild it.

### Added

- `Connection.off(event, callback)` (alias `remove_listener`, matching TS
  `protobus`) — there was previously no way to unregister an event handler.
- `MessageDispatcher.close()` and `EventDispatcher.close()`, mirroring TS
  v1.1.2. `Context.close()` now calls them before closing the connection.
- `release_amqp_resources()` / `schedule_amqp_release()` in `protobus.connection`
  — best-effort, never-raising release of a channel and its consumer, shared by
  every component that re-creates its channel on reconnection.

### Changed

- `BaseListener.init()`, `MessageDispatcher.init()` and `EventDispatcher.init()`
  are idempotent, and `_on_reconnected()` is a no-op for a component that was
  never initialized or has been closed (TS parity).

### Notes

- Tests: 70/70 pass (55 existing + 15 new reconnection-leak regression tests).
  The new tests use a counting fake connection and need no broker.
- Version stays in the 1.4.x line deliberately. This is a bug fix; the port has
  not had the security-and-stability audit that TS `protobus` 2.0 represents, so
  a 2.0 here would misrepresent it.
- The Onit services install this from PyPI (`protobus>=1.4.0`), so the fix only
  reaches running pods once 1.4.1 is published.

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
