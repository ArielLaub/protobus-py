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

- **Reconnection could deadlock itself under a flapping broker.** `_emit`
  dispatches handlers with `create_task`, so a second `reconnected` arriving
  while the first re-setup was still awaiting a round-trip ran concurrently on
  the same object; interleaved, the two re-setups each left the other's channel
  and consumer behind. Channel setup is now serialised per component.
- **A dispatcher whose callback listener failed to restore reported success.**
  `is_initialized` stays `True` once set, so the dispatcher skipped re-init
  while the callback queue was gone, and every later RPC published `reply_to`
  at a deleted queue and timed out forever. The dispatcher now gates on a new
  `BaseListener.is_ready` and re-drives the listener's `restore()`;
  `publish()` / `publish_streaming()` check readiness too.
- **A hung channel close could block recovery permanently.** Releases run on
  the restore path and are now bounded by `RELEASE_TIMEOUT_S` (5s).
- `close()` now fails pending RPCs and streams instead of leaving callers to
  wait out the full timeout — it detaches the handler that used to do that.
- `_emit` iterates a copy of the handler list, so a handler that unsubscribes
  itself no longer causes the next one to be skipped.
- Handler tasks created by `_emit` are strongly referenced, for the same reason
  the cleanup tasks are: asyncio holds only a weak reference to a running task.
- A connection that was opened but then failed to wire up is released rather
  than abandoned.
- `init()` after `close()` raises instead of silently doing nothing.

### Added

- `Connection.off(event, callback)` (alias `remove_listener`, matching TS
  `protobus`) — there was previously no way to unregister an event handler.
  Handlers are detached through `detach_listener()`, which tolerates an
  `IConnection` implemented against 1.4.0 that has no `off()`, so upgrading
  stays drop-in.
- `BaseListener.is_ready` (does it hold usable AMQP objects *right now*, as
  opposed to `is_initialized`) and `BaseListener.restore()`.
- `MessageDispatcher.close()` and `EventDispatcher.close()`, mirroring TS
  v1.1.2. `Context.close()` now calls them before closing the connection.
- `release_amqp_resources()` / `schedule_amqp_release()` in `protobus.connection`
  — best-effort, never-raising release of a channel and its consumer, shared by
  every component that re-creates its channel on reconnection.

### Changed

- **`max_reconnect_attempts` is now an escalation threshold, not a give-up
  point.** The reconnect loop keeps retrying at `max_reconnect_delay_ms` and
  emits the `error` event exactly once when the budget is exhausted. Before
  this release the loop stopped there — which was survivable only because the
  abandoned connection healed itself, i.e. because of the leak. With that leak
  fixed, giving up would leave the process permanently mute after any outage
  longer than the budget (under three minutes on the defaults). Verified
  against a real broker: a 12s outage with a 2-attempt budget never recovered
  before, and recovers now.
- `BaseListener.init()`, `MessageDispatcher.init()` and `EventDispatcher.init()`
  are idempotent, and `_on_reconnected()` is a no-op for a component that was
  never initialized or has been closed (TS parity).

### Notes

- Tests: 82/82 pass (55 existing + 27 new reconnection-leak regression tests).
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
