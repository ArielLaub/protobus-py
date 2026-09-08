"""
A streaming service that emits tokens one at a time, standing in for an LLM.

The part worth copying is ``context.signal``. Everything else here is
scaffolding to make the demo observable.
"""

import asyncio
import os
from typing import AsyncIterator

from protobus import MessageHandlerContext, MessageService


class AssistantService(MessageService):
    service_name = "Chat.Assistant"
    proto_file_name = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat.proto")

    def __init__(self, context, **options):
        super().__init__(context, **options)
        self.tokens_generated = 0
        self.stopped_early = False

    async def generate(self, request: dict, actor: str, correlation_id: str, context: MessageHandlerContext) -> AsyncIterator[dict]:
        """
        Server-streaming handler. The 4th argument carries the framework's
        per-message context; ``signal`` aborts when the caller cancels —
        because it closed its stream, passed an AbortSignal that fired, or
        went away entirely.

        Checking it is what makes cancellation *save work*. A handler that
        ignores it keeps running to the end; the framework stops publishing,
        so the caller is unaffected either way, but the work is still done
        and, for a real model, still paid for.
        """
        words = fake_completion(request["prompt"])
        delay = (request.get("token_delay_ms") or 60) / 1000
        self.tokens_generated = 0
        self.stopped_early = False
        for i, word in enumerate(words):
            if context.signal.aborted:
                # In a real service this is where you abort the upstream call
                # — cancel the HTTP request to the model provider — and let
                # the SDK tear it down.
                self.stopped_early = True
                return
            self.tokens_generated = i + 1
            yield {"index": i, "text": word}
            await asyncio.sleep(delay)

    async def stats(self, request: dict, actor: str, correlation_id: str) -> dict:
        return {"tokens_generated": self.tokens_generated, "stopped_early": self.stopped_early}


def fake_completion(prompt: str) -> list:
    """A long, deterministic "completion" so the demo always has more to say."""
    body = f'Answering "{prompt}". ' + (
        "Streaming responses arrive one token at a time, which is what lets a chat "
        "interface render text as it is produced rather than waiting for a whole "
        "reply. That same property is what makes stopping useful: when the reader "
        "has seen enough, there is no reason to keep generating, and every token "
        "after that point is wasted work on the server and wasted money at the "
        "model. This sentence continues for a while so there is always something "
        "left to cancel. "
    ) * 3
    return [f"{w} " for w in body.split()]
