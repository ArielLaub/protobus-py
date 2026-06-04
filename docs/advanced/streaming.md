# Streaming RPC

Protobus supports **server-streaming RPC** — a single request from the client can produce *many* response messages from the server, delivered as they're produced, instead of one bundled response at the end.

The motivating use case is LLM token streaming: a model generates a 500-word answer over 10 seconds, and you want to show each token to the user as it arrives rather than waiting for the full response.

> **Status:** server-streaming only (one request → many responses). Client-streaming and bidirectional streaming are not implemented and not currently planned.

## TL;DR

```python
# 1. Declare the method as streaming in your .proto file, using the gRPC `stream` keyword:
service Llm {
    rpc complete       (CompleteRequest) returns (CompleteResponse);
    rpc completeStream (CompleteRequest) returns (stream CompleteChunk);
}

# 2. Server: write an async generator that yields each chunk
class LlmService(MessageService):
    async def completeStream(self, req, actor, correlation_id):
        async for event in bedrock.converse_stream(req):
            yield {"delta": event.text}
        yield {"stop_reason": "end_turn", "usage": event.usage}

# 3. Client: iterate with `async for`
async for chunk in llm_proxy.completeStream({"prompt": "..."}):
    print(chunk["delta"], end="", flush=True)
```

The framework handles correlation IDs, the reply queue, end-of-stream detection, error propagation, and cancellation. You write the generator.

## When to use streaming

Use streaming when:

- The response is **incrementally meaningful** — each chunk is useful before the next arrives (LLM tokens, log tails, video frames, progress updates).
- The response **takes too long** to deliver as one blob — users perceive latency by *time to first byte*, not by total response time.
- You want to **cancel** cleanly — closing an iterator unwinds the work upstream.

Don't use streaming when:

- The chunks are tiny and the response is fast — adding stream overhead just to deliver 50 bytes hurts more than it helps.
- The client always needs the full response before doing anything — pagination over unary calls is simpler.
- The data is **not naturally ordered** — streaming guarantees in-order delivery within a single call, which costs flexibility you might not want.

## Wire protocol

A streaming response is **N+1 AMQP messages** published to the client's reply queue, all carrying the same `correlation_id` as the request. End-of-stream is signaled by an AMQP **header** on the final message; the message body is a regular response payload like any other.

### Per-message headers

| Header | Type | Required | Meaning |
|---|---|---|---|
| `x-protobus-final` | `bool` | yes (on terminal) | `false` (or absent) → more messages follow. `true` → this is the last chunk. |
| `x-protobus-seq` | `uint32` | optional | Monotonically increasing 0-based sequence. Useful for diagnostics; not required for correctness (RabbitMQ guarantees order within the single-publisher → single-queue → single-consumer topology of an RPC reply). |

The standard AMQP `correlation_id` is reused exactly as for unary calls — it ties every chunk back to the request that initiated the stream.

### Why headers, not the payload

Streaming markers are **transport-layer concerns**, not application data. Keeping them on AMQP headers means:

- `ResponseContainer` stays semantically clean — it's "result OR error", not "result OR error PLUS streaming state".
- Adding new transport flags later (cancel, ack, window) costs nothing — no proto bump.
- Old unary clients never see streaming concepts they don't understand.
- The same call site code works whether the framework batches one message or a hundred.

### End-of-stream rules

The terminal message carries `x-protobus-final: true`. Its body is a regular response container — typically containing the last data chunk (e.g., the final `delta` plus `stop_reason` and `usage` for an LLM call), but it can also be empty or an error.

Three terminal outcomes the client must handle:

1. **Normal completion** — `x-protobus-final: true` + a result payload. Iterator yields the final chunk and stops.
2. **Mid-stream error** — `x-protobus-final: true` + an error payload. Iterator raises the error.
3. **Timeout / disconnect** — no terminal message arrives within the idle timeout. Iterator raises `StreamTimeoutError`.

## Declaring a streaming method

Use the standard gRPC syntax — the `stream` keyword on the response type:

```proto
service Llm {
    rpc complete       (CompleteRequest) returns (CompleteResponse);
    rpc completeStream (CompleteRequest) returns (stream CompleteChunk);
    //                                            ^^^^^^^^^^^^^^^^^^^
}

message CompleteChunk {
    string delta       = 1;   // incremental text (empty on terminal chunk if no more text)
    string stop_reason = 2;   // populated on terminal chunk
    Usage  usage       = 3;   // populated on terminal chunk
}
```

Protobus reads the `server_streaming` flag from the method's `MethodDescriptorProto` at startup — no custom parser, no convention, no annotation. If you've used gRPC, this is the same syntax.

The proxy and the service base class inspect this flag once when methods are wired up:

- If `server_streaming = false` → the proxy generates an `async def` method that returns the decoded response (current behavior, unchanged).
- If `server_streaming = true` → the proxy generates an `async def` method that returns an `AsyncIterator` of decoded chunks.

## Client API

The proxy method returns an `AsyncIterator[T]` — you consume it with `async for`:

```python
from protobus import ServiceProxy

llm = ServiceProxy(ctx, "Llm.Service")
await llm.init()

async for chunk in llm.completeStream({"prompt": "tell me about life insurance"}):
    print(chunk["delta"], end="", flush=True)
```

That's the entire client API for streaming. The framework:

1. Publishes the request once.
2. Drains reply-queue messages matching the `correlation_id`, decoding each.
3. Yields each decoded chunk to the loop.
4. When it sees `x-protobus-final: true`, yields the final chunk (if any) and ends the iterator.
5. If the terminal message carries an error, raises it out of the `async for`.

### Error handling

Errors are raised inside the iteration — same model as any async generator:

```python
from protobus.errors import HandledError, StreamTimeoutError

try:
    async for chunk in llm.completeStream(req):
        process(chunk)
except HandledError as e:
    # Server returned a known error mid-stream (guardrail block, validation, etc.)
    logger.warning("stream aborted: %s (code=%s)", e.message, e.code)
except StreamTimeoutError:
    # No chunk for STREAM_IDLE_TIMEOUT_MS (default 60_000)
    logger.error("stream went silent")
except Exception:
    # Unexpected transport error
    raise
```

### Early termination

Break out of the loop — the iterator's `__aexit__` (via `aclose()`) tells the framework to stop draining the reply queue and release resources:

```python
async for chunk in llm.completeStream(req):
    if user_cancelled.is_set():
        break
    process(chunk)
```

In v1, this releases client-side resources but does **not** signal the server to stop generating. Server-side cancellation is on the roadmap (see [Limitations](#limitations)).

### Timeouts

Streaming uses an **idle timeout** rather than a total-call timeout, because a long stream can legitimately take minutes. The default is 60 seconds between chunks (configurable via `STREAM_IDLE_TIMEOUT_MS`):

```python
# Per-call override
async for chunk in llm.completeStream(req, stream_idle_timeout_ms=120_000):
    ...
```

If no chunk arrives within the timeout, `StreamTimeoutError` is raised. The unary `request_timeout_ms` does not apply to streaming calls.

## Server API

A streaming handler is an **async generator** (`yield`s instead of `return`s):

```python
from typing import AsyncIterator
from protobus import MessageService

class LlmService(MessageService):
    service_name = "Llm.Service"
    proto_filename = "llm.proto"

    async def completeStream(
        self,
        request: dict,
        actor: str,
        correlation_id: str,
    ) -> AsyncIterator[dict]:
        # Stream chunks as they arrive from upstream
        async for event in bedrock.converse_stream(
            model=request["model_id"],
            messages=request["messages"],
        ):
            yield {"delta": event.text}

        # Terminal chunk carries finalization metadata
        yield {
            "stop_reason": event.stop_reason,
            "usage": event.usage,
        }
```

The framework:

1. Detects that the handler is an async generator (`inspect.isasyncgenfunction`).
2. For each yielded value: encodes a response container, publishes to `reply_to` with `x-protobus-final: false` and an incrementing `x-protobus-seq`.
3. When the generator exhausts, publishes a final empty terminal message with `x-protobus-final: true` and the last seq+1 — or, if the last yielded value was already prepared as terminal (see below), promotes that publish instead.

### Marking the last yield as terminal

By default, the framework publishes an empty terminal message after your generator exhausts. If you want your last `yield` to carry the final payload (typical for LLM `stop_reason` / `usage`), the framework automatically promotes it: when the generator returns immediately after a yield, that yield's published message gets `x-protobus-final: true` instead of being followed by an empty terminal.

In practice you don't need to do anything special — just yield your terminal data last and return:

```python
async def completeStream(self, req, actor, cid):
    async for event in bedrock.converse_stream(req):
        yield {"delta": event.text}             # x-protobus-final: false
    yield {"stop_reason": "end_turn", "usage": ...}  # x-protobus-final: true (auto-promoted)
```

### Raising errors mid-stream

Raising from inside the generator publishes a terminal error message that the client iterator will re-raise:

```python
async def completeStream(self, req, actor, cid):
    async for event in bedrock.converse_stream(req):
        if guardrail.flagged(event.text):
            raise HandledError("guardrail blocked output", code="GUARDRAIL_BLOCKED")
        yield {"delta": event.text}
```

`HandledError` skips retry/DLQ logic the same way it does for unary calls.

## Backpressure

The reply queue is a per-client anonymous queue (auto-delete, exclusive). If the client iterates slowly, messages buffer there.

To bound memory under pathological cases, streaming reply queues are declared with:

- `x-max-length: 1000` — drop oldest when full (configurable via `STREAM_QUEUE_MAX_LENGTH`)
- `x-message-ttl: 600_000` — per-message TTL of 10 minutes

If you hit either limit, the iterator raises `StreamBackpressureError`. For chat token streaming neither limit is reachable in practice; both are safety nets.

## Backward compatibility

The streaming feature is **purely additive**:

- **Existing unary RPCs are unchanged.** No proto changes, no API changes, no header changes. The framework only inspects the streaming flag when wiring up a method, and unary methods follow the exact same path they did before.
- **Old clients calling new unary methods** — works, no change.
- **Old clients calling new streaming methods** — the proxy method is no longer awaitable in the old style. This is a compile-time / runtime API change you opt into per-method by adding `stream` to your `.proto`.
- **New clients calling old unary methods** — works, no change.
- **Mixed-version services in the same cluster** — fine, as long as the *individual method* contract agrees on whether it's streaming.

## Comparison with gRPC

Protobus streaming intentionally mirrors gRPC's server-streaming model so the mental model ports:

| | gRPC server-streaming | Protobus server-streaming |
|---|---|---|
| Proto syntax | `returns (stream Foo)` | Identical |
| Client API (Python) | `for chunk in stub.Foo(req)` | `async for chunk in proxy.Foo(req)` |
| Transport | HTTP/2 with stream frames | AMQP with multiple replies on a correlation_id |
| Ordering guarantee | Per-stream FIFO | Per-stream FIFO (RabbitMQ single-queue/single-consumer) |
| End-of-stream signal | HTTP/2 END_STREAM frame | `x-protobus-final: true` header |
| Cancellation | Client closes the stream | v1: client unwinds locally. Server cancellation: roadmap. |
| Client-streaming / bidi | Supported | Not supported, not planned |

The biggest practical difference: gRPC streams ride on HTTP/2's multiplexed connection, so the cost per stream is low and you can have thousands open. Protobus rides on a single AMQP reply queue per client, multiplexed by `correlation_id` — the per-stream cost is the same as a unary call, but very-high-fanout topologies should be benchmarked.

## Limitations

- **Server-streaming only.** Client-streaming and bidirectional streaming aren't supported.
- **No server-side cancellation in v1.** When a client breaks out of the iterator, the server keeps generating until its own generator exhausts. Wasted upstream work, but no correctness problem. Roadmap: a `<correlation_id>.cancel` sentinel queue.
- **No exactly-once semantics.** If RabbitMQ requeues a chunk during failover, the client may see duplicates. The framework provides no dedup. For idempotent chunks (LLM deltas, log lines) this is fine; for non-idempotent chunks, the caller is responsible.
- **No chunk-level retry/DLQ.** Standard retry/DLQ applies to the entire RPC, not to individual chunks. A mid-stream failure that requeues will restart the stream from chunk 0.
- **Single reply queue per client.** All in-flight streams to a single proxy share one reply queue. Very-high-concurrency callers may want multiple proxy instances.

## Implementation notes

For framework contributors. Skip if you're just using streaming.

The streaming path differs from unary in four places:

1. **`MessageFactory._is_streaming_method(method_name)`** — looks up `method_descriptor.server_streaming` from the descriptor pool. Cached.

2. **`ServiceProxy._create_method(method_name)`** — at proxy-build time, branches on the streaming flag. Streaming methods are exposed as functions that return `AsyncIterator[T]` rather than coroutines that return `T`.

3. **`MessageDispatcher`** — adds `_pending_streams: Dict[correlation_id, asyncio.Queue]`. The callback listener pushes incoming chunks into the queue; the async iterator pops them out. The terminal message (`x-protobus-final: true`) signals "close the queue."

4. **`MessageService._on_message()`** — detects `inspect.isasyncgenfunction(handler)`. Iterates the generator, publishing each yield with appropriate headers. Catches exceptions and publishes them as terminal error messages.

The wire format itself uses **only AMQP headers** — no `ResponseContainer` schema changes. This is what makes the feature purely additive.

## See also

- [Message Flow](../message-flow.md) — the underlying unary RPC pipeline this builds on
- [Error Handling](error-handling.md) — `HandledError` and retry semantics, which apply identically to streaming
- [Configuration](../configuration.md) — `STREAM_IDLE_TIMEOUT_MS`, `STREAM_QUEUE_MAX_LENGTH` settings
