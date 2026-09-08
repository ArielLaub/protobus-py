#!/usr/bin/env python3
"""
Streaming with cancellation, in the shape a chat UI needs.

Run it with a broker up::

    docker compose up -d
    python -m sample.tokenStream.streaming_demo
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from protobus import AbortController, Context, LogLevel, ServiceProxy, StreamOptions, set_log_level  # noqa: E402

from sample.tokenStream.assistant_service import AssistantService  # noqa: E402

AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/")
HERE = os.path.dirname(os.path.abspath(__file__))


async def main() -> None:
    set_log_level(LogLevel.Warn)
    context = Context()
    await context.init(AMQP_URL, [HERE])

    # max_concurrent is the consumer prefetch, and it defaults to 1. A
    # streaming handler holds its slot for the whole life of the stream, so
    # leaving the default would serve one caller at a time.
    service = AssistantService(context, max_concurrent=8)
    await service.init()

    assistant = ServiceProxy(context, service.service_name)
    await assistant.init()

    await demo_stop_button(assistant, service)
    await demo_close(assistant, service)
    await demo_run_to_completion(assistant, service)

    await service.close()
    await context.close()


async def demo_stop_button(assistant, service):
    """The chat-window case: Stop lives outside the loop and must take
    effect immediately rather than at the next token."""
    header("1. Stop button (AbortSignal)")
    stop = AbortController()

    # Whatever your Stop endpoint is, this is all it does. In an HTTP server
    # you would keep the controller keyed by conversation id.
    async def press_stop():
        await asyncio.sleep(0.9)
        print("\n  [user pressed Stop]")
        stop.abort()

    asyncio.ensure_future(press_stop())
    print("  ", end="")
    try:
        async for token in assistant.generate(
            {"prompt": "why does streaming matter?", "token_delay_ms": 60},
            None, None, StreamOptions(signal=stop.signal),
        ):
            print(token["text"], end="", flush=True)
    except Exception:
        # A stream abandoned mid-flight may surface as a throw; the loop is
        # over either way, which is what the UI cares about.
        if not stop.signal.aborted:
            raise
    await report_server_side(assistant, service)


async def demo_close(assistant, service):
    """The simple case: the decision is made inside the loop. Closing the
    stream — `async with`, or `await stream.aclose()` — is what tells the
    server; a bare `break` alone does not close a Python async iterator."""
    header("2. close the stream from inside the loop")
    printed = 0
    print("  ", end="")
    async with assistant.generate({"prompt": "what if I only want the first few words?", "token_delay_ms": 60}) as stream:
        async for token in stream:
            print(token["text"], end="", flush=True)
            printed += 1
            if printed == 8:
                break
    print("\n  [consumer stopped reading]")
    await report_server_side(assistant, service)


async def demo_run_to_completion(assistant, service):
    """The control: nothing cancels, so the server produces everything."""
    header("3. no cancellation")
    received = 0
    async for _token in assistant.generate({"prompt": "short answer", "token_delay_ms": 1}):
        received += 1
    print(f"  received {received} tokens")
    await report_server_side(assistant, service)


async def report_server_side(assistant, service):
    """The whole point: ask the server what it actually did."""
    await asyncio.sleep(0.4)
    stats = await assistant.stats({})
    print(f"  server generated {stats['tokens_generated']} tokens; stopped early: {stats['stopped_early']}")
    print(f"  (in-process check: {service.tokens_generated} generated, stopped_early={service.stopped_early})")


def header(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


if __name__ == "__main__":
    asyncio.run(main())
