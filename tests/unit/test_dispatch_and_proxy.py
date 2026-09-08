"""Dispatch is bound to the service contract; ServiceProxy addresses instance
names and preserves error identity."""

import pytest

from protobus import (
    ChannelClosedError,
    DisconnectedError,
    HandledError,
    InvalidRequestError,
    InvalidResponseError,
    InvalidServiceNameError,
    MessageFactory,
    MessageService,
    PublishConfirmTimeoutError,
    PublishNackedError,
    RemoteError,
    RpcTimeoutError,
    ServiceProxy,
    UnroutableError,
)
from protobus.connection import AbortController, MessageHandlerContext
from protobus.message_factory import _RequestContainerMsg

from ..helpers import FakeContext, make_factory

TARGET = """
syntax = "proto3";
package Combat;
service Player {
  rpc shoot(ShootRequest) returns (ShootResponse);
  rpc stop_consuming(ShootRequest) returns (ShootResponse);
}
message ShootRequest { string target = 1; }
message ShootResponse { bool hit = 1; }
"""

OTHER = """
syntax = "proto3";
package Other;
service Service { rpc shoot(OtherShoot) returns (OtherResponse); }
message OtherShoot { string attacker_field = 1; int32 privileged = 2; }
message OtherResponse { bool ok = 1; }
"""


class Player(MessageService):
    player_id = "player6"

    @property
    def service_name(self):
        return f"Combat.Player.{self.player_id}"

    proto_file_name = "Combat.proto"

    @property
    def Proto(self):
        return TARGET

    def __init__(self, context):
        super().__init__(context)
        self.shots = []

    async def shoot(self, req, *_a):
        self.shots.append(req)
        return {"hit": True}

    # `stop_consuming` is declared in the .proto but deliberately NOT
    # implemented here: it must not fall through to the framework's member.


def forge(factory, real_method, forged_method, obj):
    """Re-encode a request with an attacker-chosen method."""
    real = _RequestContainerMsg.FromString(factory.build_request(real_method, obj, "attacker"))
    return _RequestContainerMsg(method=forged_method, actor=real.actor, data=real.data).SerializeToString()


def build():
    factory = make_factory(TARGET, "Combat.Player")
    factory.parse(OTHER, "Other.Service")
    ctx = FakeContext(factory)
    return factory, Player(ctx), ctx


def rk(routing_key):
    return MessageHandlerContext(signal=AbortController().signal, routing_key=routing_key)


class TestRequestDispatchIsBoundToTheServiceContract:
    async def test_serves_a_legitimate_call_under_a_runtime_name_that_differs_from_the_contract(self):
        factory, svc, _ = build()
        body = factory.build_request("Combat.Player.shoot", {"target": "bob"}, "alice")
        out = await svc._on_message(body, "cid", {}, rk("REQUEST.Combat.Player.player6.shoot"))
        assert svc.shots == [{"target": "bob"}]
        assert factory.decode_response(out).result.data == {"hit": True}
        assert svc.contract_service_name == "Combat.Player"

    async def test_rejects_a_trailing_segment_that_renames_the_dispatch_target(self):
        factory, svc, ctx = build()
        body = forge(factory, "Combat.Player.shoot", "Combat.Player.shoot.publish_event", {"target": "bob"})
        out = await svc._on_message(body, "cid", {}, rk("REQUEST.Combat.Player.player6.publish_event"))
        assert ctx.events == []
        assert factory.decode_response(out).error is not None

    async def test_rejects_a_body_whose_method_belongs_to_another_service(self):
        factory, svc, _ = build()
        body = forge(factory, "Other.Service.shoot", "Other.Service.shoot", {"attacker_field": "pwn", "privileged": 7})
        out = await svc._on_message(body, "cid", {}, rk("REQUEST.Combat.Player.player6.shoot"))
        assert svc.shots == []
        assert factory.decode_response(out).error is not None

    async def test_does_not_fall_through_to_a_framework_member_for_a_declared_but_unimplemented_rpc(self):
        factory, svc, _ = build()
        calls = []
        original = MessageService.stop_consuming

        async def spy(self):
            calls.append(1)
            return await original(self)

        MessageService.stop_consuming = spy  # type: ignore[method-assign]
        try:
            body = factory.build_request("Combat.Player.stop_consuming", {"target": "x"}, "attacker")
            out = await svc._on_message(body, "cid", {}, rk("REQUEST.Combat.Player.player6.stop_consuming"))
        finally:
            MessageService.stop_consuming = original  # type: ignore[method-assign]
        assert calls == []
        assert factory.decode_response(out).error is not None

    async def test_rejects_a_mismatch_between_the_routing_key_and_the_body_method(self):
        factory, svc, _ = build()
        body = factory.build_request("Combat.Player.shoot", {"target": "bob"}, "alice")
        out = await svc._on_message(body, "cid", {}, rk("REQUEST.Combat.Player.player6.stop_consuming"))
        assert svc.shots == []
        assert factory.decode_response(out).error is not None

    async def test_still_validates_the_body_when_no_routing_key_is_available(self):
        factory, svc, ctx = build()
        body = forge(factory, "Combat.Player.shoot", "Combat.Player.shoot.publish_event", {"target": "bob"})
        out = await svc._on_message(body, "cid", {}, None)
        assert ctx.events == []
        assert factory.decode_response(out).error is not None

    async def test_a_private_name_is_never_a_handler(self):
        factory, svc, _ = build()
        assert svc._resolve_own_handler("_on_message") is None
        assert svc._resolve_own_handler("init") is None
        assert svc._resolve_own_handler("shoot") is not None

    async def test_the_handler_receives_the_context_when_it_asks_for_it(self):
        factory = make_factory(TARGET, "Combat.Player")
        seen = {}

        class P(MessageService):
            service_name = "Combat.Player"
            proto_file_name = "x"
            Proto = TARGET

            async def shoot(self, req, actor, correlation_id, context):
                seen.update(req=req, actor=actor, correlation_id=correlation_id, context=context)
                return {"hit": False}

        svc = P(FakeContext(factory))
        ctx = rk("REQUEST.Combat.Player.shoot")
        await svc._on_message(factory.build_request("Combat.Player.shoot", {"target": "t"}, "alice"), "c-1", {}, ctx)
        assert seen == {"req": {"target": "t"}, "actor": "alice", "correlation_id": "c-1", "context": ctx}

    async def test_a_sync_handler_is_accepted(self):
        factory = make_factory(TARGET, "Combat.Player")

        class P(MessageService):
            service_name = "Combat.Player"
            proto_file_name = "x"
            Proto = TARGET

            def shoot(self, req, *_a):
                return {"hit": True}

        out = await P(FakeContext(factory))._on_message(factory.build_request("Combat.Player.shoot", {}, "a"), "c", {})
        assert factory.decode_response(out).result.data == {"hit": True}

    async def test_a_missing_contract_is_a_missing_proto(self):
        from protobus import MissingProto

        factory = MessageFactory()
        factory.init([])

        class Lost(MessageService):
            service_name = "Nowhere.Svc"
            proto_file_name = "x"
            Proto = 'syntax = "proto3"; package Elsewhere; service Svc {}'

        with pytest.raises(MissingProto):
            await Lost(FakeContext(factory)).init()


PROXY_PROTO = """syntax = "proto3";
package Combat;
message ShootRequest { string target = 1; }
message ShootResponse { bool hit = 1; }
service Player { rpc shoot(Combat.ShootRequest) returns(Combat.ShootResponse); }"""


def proxy_context():
    factory = make_factory(PROXY_PROTO, "Combat.Player")
    ctx = FakeContext(factory)
    ctx.reply = factory.build_response("Combat.Player.shoot", {"hit": True})
    return ctx


class TestServiceProxyAgainstAnInstanceNamedService:
    async def test_initialises_against_the_contract_the_runtime_name_is_an_instance_of(self):
        proxy = ServiceProxy(proxy_context(), "Combat.Player.player6")
        await proxy.init()
        assert callable(proxy.shoot)
        assert proxy.contract_service_name == "Combat.Player"

    async def test_routes_to_the_instance_not_to_the_contract(self):
        ctx = proxy_context()
        proxy = ServiceProxy(ctx, "Combat.Player.player6")
        await proxy.init()
        assert await proxy.shoot({"target": "player7"}) == {"hit": True}
        assert ctx.published[0]["routing_key"] == "REQUEST.Combat.Player.player6.shoot"

    async def test_names_the_contract_method_in_the_envelope(self):
        ctx = proxy_context()
        proxy = ServiceProxy(ctx, "Combat.Player.player6")
        await proxy.init()
        await proxy.shoot({"target": "player7"}, "alice")
        envelope = ctx.factory.decode_request_envelope(ctx.published[0]["content"])
        assert envelope.method == "Combat.Player.shoot"
        assert envelope.actor == "alice"
        assert ctx.factory.decode_request_payload(envelope.method, envelope.data) == {"target": "player7"}

    async def test_still_works_for_a_plain_contract_name(self):
        ctx = proxy_context()
        proxy = ServiceProxy(ctx, "Combat.Player")
        await proxy.init()
        await proxy.shoot({"target": "player7"})
        assert ctx.published[0]["routing_key"] == "REQUEST.Combat.Player.shoot"

    async def test_still_refuses_a_name_that_matches_no_contract_at_any_prefix(self):
        with pytest.raises(InvalidServiceNameError):
            await ServiceProxy(proxy_context(), "Combat.Referee.ref1").init()

    async def test_refuses_to_initialise_before_the_factory(self):
        ctx = FakeContext(MessageFactory())
        with pytest.raises(InvalidServiceNameError, match="init"):
            await ServiceProxy(ctx, "Combat.Player").init()

    async def test_refuses_to_initialise_twice(self):
        from protobus import AlreadyInitializedError

        proxy = ServiceProxy(proxy_context(), "Combat.Player")
        await proxy.init()
        with pytest.raises(AlreadyInitializedError):
            await proxy.init()

    async def test_refuses_a_method_that_collides_with_a_proxy_member(self):
        factory = make_factory('syntax = "proto3"; package C; message M {} service S { rpc init(M) returns (M); }', "C.S")
        with pytest.raises(InvalidServiceNameError, match="collides"):
            await ServiceProxy(FakeContext(factory), "C.S").init()

    async def test_a_non_rpc_call_returns_an_empty_dict(self):
        ctx = proxy_context()
        proxy = ServiceProxy(ctx, "Combat.Player")
        await proxy.init()
        assert await proxy.shoot({"target": "x"}, None, False) == {}
        assert ctx.published[0]["rpc"] is False

    async def test_passes_timeout_and_options_through(self):
        from protobus import CallOptions

        ctx = proxy_context()
        proxy = ServiceProxy(ctx, "Combat.Player")
        await proxy.init()
        await proxy.shoot({"target": "x"}, "me", True, 1234, CallOptions(priority=1, message_id="m"))
        assert ctx.published[0]["timeout_ms"] == 1234
        assert ctx.published[0]["options"].priority == 1
        await proxy.shoot({"target": "x"}, priority=2, message_id="n")
        assert ctx.published[1]["options"].priority == 2
        assert ctx.published[1]["options"].message_id == "n"


P_PROTO = """
syntax = "proto3";
package P;
service S { rpc go(Req) returns (Res); }
message Req { string a = 1; }
message Res { bool ok = 1; }
"""


async def proxy_that_fails_with(thrown):
    ctx = FakeContext(make_factory(P_PROTO, "P.S"))

    async def fail(*_a):
        raise thrown

    ctx.reply = fail
    proxy = ServiceProxy(ctx, "P.S")
    await proxy.init()
    return proxy


class TestServiceProxyPreservesDeliveryErrors:
    @pytest.mark.parametrize("thrown", [
        UnroutableError("nothing bound", "mid-1"),
        PublishNackedError("broker refused", "mid-2"),
        PublishConfirmTimeoutError("no confirm", "mid-3"),
        ChannelClosedError("channel went", "mid-4"),
        RpcTimeoutError("no reply"),
        DisconnectedError(),
    ])
    async def test_rethrows_with_its_identity_intact(self, thrown):
        proxy = await proxy_that_fails_with(thrown)
        with pytest.raises(type(thrown)) as info:
            await proxy.go({"a": "x"})
        assert info.value is thrown

    async def test_keeps_code_and_message_id_reachable_for_a_dedup_decision(self):
        proxy = await proxy_that_fails_with(PublishConfirmTimeoutError("no confirm", "mid-9"))
        with pytest.raises(PublishConfirmTimeoutError) as info:
            await proxy.go({"a": "x"})
        assert info.value.code == "PUBLISH_CONFIRM_TIMEOUT"
        assert info.value.message_id == "mid-9"

    async def test_still_raises_a_local_encode_failure_as_invalid_request_error(self):
        ctx = FakeContext(make_factory(P_PROTO, "P.S"))
        ctx.reply = b""
        proxy = ServiceProxy(ctx, "P.S")
        await proxy.init()
        with pytest.raises(InvalidRequestError, match="failed parsing message"):
            await proxy.go({"a": ["not", "a", "string"]})
        assert ctx.published == []

    async def test_surfaces_a_remote_handled_error_with_its_code(self):
        factory = make_factory(P_PROTO, "P.S")
        ctx = FakeContext(factory)
        ctx.reply = factory.build_response("P.S.go", HandledError("nope", "VALIDATION"))
        proxy = ServiceProxy(ctx, "P.S")
        await proxy.init()
        with pytest.raises(RemoteError) as info:
            await proxy.go({"a": "x"})
        assert info.value.message == "nope"
        assert info.value.code == "VALIDATION"
        assert info.value.method == "P.S.go"

    async def test_an_undecodable_reply_is_an_invalid_response(self):
        ctx = FakeContext(make_factory(P_PROTO, "P.S"))
        ctx.reply = b"\xff\xff\xff"
        proxy = ServiceProxy(ctx, "P.S")
        await proxy.init()
        with pytest.raises(InvalidResponseError):
            await proxy.go({"a": "x"})

    async def test_a_reply_with_neither_result_nor_error_is_an_invalid_response(self):
        from protobus.message_factory import _ResponseContainerMsg

        ctx = FakeContext(make_factory(P_PROTO, "P.S"))
        ctx.reply = _ResponseContainerMsg().SerializeToString()
        proxy = ServiceProxy(ctx, "P.S")
        await proxy.init()
        with pytest.raises(InvalidResponseError, match="neither"):
            await proxy.go({"a": "x"})
