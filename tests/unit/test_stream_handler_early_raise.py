"""A streaming handler that raises before returning its iterator answers the
caller exactly as the unary path answers a raise.

Parity with TypeScript protobus 2.5.0 (protobus#29), where such a raise
escaped dispatch and the caller waited out its whole idle timeout. This port
always handled it; these tests keep it that way.
"""

import pytest

from protobus import HandledError, MessageService
from protobus.connection import RESPONSE_BUFFER_ATTR, AbortController, MessageHandlerContext

from ..helpers import FakeContext, make_factory

PROTO = """syntax = "proto3";
package Feed;
service Ticker {
  rpc validated(Req) returns (stream Tick);
  rpc crashes(Req) returns (stream Tick);
}
message Req { int32 n = 1; }
message Tick { int32 i = 1; }
"""


class Ticker(MessageService):
    service_name = "Feed.Ticker"
    proto_file_name = "Feed.proto"

    @property
    def Proto(self):
        return PROTO

    # Plain functions, not generators: each raises while producing its iterator.
    def validated(self, req, *_a):
        if req.get("n", 0) < 1:
            raise HandledError("n must be positive", "INVALID_ARGUMENT")
        return self._ticks(req["n"])

    def crashes(self, req, *_a):
        raise RuntimeError("internal detail: db password rejected")

    async def _ticks(self, n):
        for i in range(n):
            yield {"i": i}


def build():
    factory = make_factory(PROTO, "Feed.Ticker")
    return factory, Ticker(FakeContext(factory))


async def dispatch(svc, factory, method, n):
    body = factory.build_request(f"Feed.Ticker.{method}", {"n": n}, "tester")
    ctx = MessageHandlerContext(signal=AbortController().signal, routing_key=f"REQUEST.Feed.Ticker.{method}")
    return await svc._on_message(body, "cid", {}, ctx)


class TestAStreamingHandlerThatRaisesBeforeItsIterator:
    async def test_answers_a_handled_error_at_once(self):
        factory, svc = build()
        out = await dispatch(svc, factory, "validated", 0)
        assert isinstance(out, bytes)
        error = factory.decode_response(out).error
        assert error.message == "n must be positive"
        assert error.code == "INVALID_ARGUMENT"

    @pytest.mark.parametrize("expose, leaks", [("true", True), ("false", False)])
    async def test_fails_an_unexpected_error_with_the_terminal_reply(self, monkeypatch, expose, leaks):
        monkeypatch.setenv("PROTOBUS_EXPOSE_INTERNAL_ERRORS", expose)
        factory, svc = build()
        with pytest.raises(RuntimeError) as raised:
            await dispatch(svc, factory, "crashes", 1)
        reply = getattr(raised.value, RESPONSE_BUFFER_ATTR)
        error = factory.decode_response(reply).error
        assert error is not None
        assert ("db password" in error.message) is leaks

    async def test_still_streams_when_the_handler_returns_its_iterator(self):
        factory, svc = build()
        out = await dispatch(svc, factory, "validated", 2)
        frames = [factory.decode_response(f).result.data async for f in out]
        assert frames == [{"i": 0}, {"i": 1}]
