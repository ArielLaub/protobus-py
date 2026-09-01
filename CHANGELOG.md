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
  alone, because ordering there is by TTL expiry rather than priority.

- **Retries and DLQ now carry the message's priority.**
  `Connection._retry_message` re-publishes rather than relying on the broker's
  dead-lettering, so it was silently dropping `priority` — a control message
  that failed once came back at priority 0 and queued behind the whole bulk
  backlog, which is the exact failure priority exists to prevent and is only
  visible after something has already gone wrong. `_send_to_dlq` copies it too:
  no ordering value on a plain queue, but a DLQ exists to preserve what the
  message was. Anywhere protobus re-publishes instead of letting the broker
  move a message, priority has to be carried by hand.

- **Priority reorders the queue, not the consumers' prefetch buffers.** With
  prefetch `N` across `R` replicas, up to `N × R` messages can still sit ahead
  of a high-priority one. It shrinks the window by orders of magnitude; it does
  not eliminate it.

  **While the consumer is saturated** — every prefetch slot occupied by an
  in-flight handler — the relationship is an exact equality. Measured on one
  replica with a 50-message backlog and only the prefetch varying:
  `max_concurrent` of 1/5/20 puts the control message at index 1/5/20, and
  `max_concurrent=100` puts it at index 50 — the whole backlog was prefetched
  and **priority is inert**. So `max_concurrent` is not an independent
  throughput dial once priority is in play: raising it widens the window
  priority cannot see into by exactly the amount you raise it.

  The saturation condition is load-bearing and is stated in the docs, because
  protobus does not serialise deliveries — a free prefetch slot keeps pulling
  from the queue while other handlers run. With a **fast** handler the backlog
  drains itself and there is nothing left to jump: same setup holding only the
  first delivery, prefetch 5 and 20 both consumed all 50 bulk messages before
  the control message was published. That measures the drain rate, not the
  prefetch window. The test asserts peak in-flight == prefetch so the
  precondition is encoded rather than assumed.

- **`max_priority` requires `max_concurrent` (and `late_ack`), and is refused
  without them.** With no prefetch bound the broker pushes the whole queue into
  the consumer's buffer and priority does nothing whatsoever. Measured — 300
  bulk messages arriving while the consumer is already draining, then one
  control message: `max_concurrent=1, late_ack=True` handled it at position 92
  of 301; `max_concurrent=1, late_ack=False` and `max_concurrent=None` both
  handled it at position 300 of 301. `late_ack` matters because RabbitMQ
  ignores QoS prefetch for auto-ack consumers. This raises
  `InvalidPriorityError` at construction rather than warning, because the
  failure is otherwise invisible: the queue is correctly declared, the operator
  has done the migration, and the feature is simply inert. Via
  `MessageServiceOptions` only `max_concurrent` is needed — `MessageService`
  sets `late_ack` itself.

- **Port parity, verified across the wire.** The TypeScript port gains the same
  feature in the same release, with the same concepts and wire behaviour
  (`maxPriority` / `priority`, the same three constants, the same validation
  range). The TS port's integration harness drove a Python server from a TS
  client against a live broker and confirmed both halves that matter: a TS
  `priority` is honoured by a Python `max_priority` queue (control message
  handled at index 1 of 21, with the consumer already draining), and the two
  ports emit an **identical queue argument set** — a TS service re-declares a
  Python-created priority queue with no 406, while the negative control (a TS
  service without `maxPriority`) is correctly rejected. The reverse direction,
  Python publisher → TS consumer, was then run too: control handled at index 1
  of 21, identical to the forward direction. Both directions verified, not
  inferred.

  Three deliberate divergences, all behaviourally invisible because RabbitMQ
  cannot distinguish an absent priority from an explicit `0`: amqplib omits the
  property when unset whereas aio-pika normalizes it to `0` (as it always has,
  before this change); an explicit `priority=0` is forwarded by TS but folded
  into the default path by Python, where the bytes are identical either way and
  folding preserves compatibility with a third-party `IContext` predating the
  parameter; and TS defaults its prefetch so it need only reject
  `lateAck: false`, whereas Python has no default prefetch and rejects both.
  See `docs/advanced/message-priority.md`.

- Validation exists because neither underlying failure is usable as-is:
  aio-pika applies `int()` to a priority, so `1.5` would be silently stored as
  `1`; an out-of-range value surfaces as a raw `struct.error` from the encoder;
  and an out-of-range `x-max-priority` is a channel-killing 406.

- Tests: 118/118 pass (55 existing + 63 priority). The broker-backed ones need
  a live RabbitMQ (`docker-compose up -d`).
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
