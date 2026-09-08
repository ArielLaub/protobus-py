"""EventListener dispatch, the topic Trie, and Config parsing."""

import pytest

from protobus import Config, EventListener, EventRetryOptions, MessageFactory, Trie
from protobus.connection import AbortController, MessageHandlerContext

from ..helpers import FakeConnection, make_factory

EVENT_PROTO = 'syntax = "proto3"; package Ev; message TestEvent { string message = 1; } message Other { int32 n = 1; }'


def routed(routing_key):
    return MessageHandlerContext(signal=AbortController().signal, routing_key=routing_key)


class TestEventListener:
    async def listener(self, retry=None):
        conn = FakeConnection()
        listener = EventListener(conn, make_factory(EVENT_PROTO), retry)
        await listener.init(None, "test-queue")
        return conn, listener

    async def test_calls_an_event_handler_exactly_once_per_matching_subscription(self):
        conn, listener = await self.listener()
        calls = []

        async def handler(data, event_type, topic):
            calls.append((data, event_type, topic))

        await listener.subscribe("Ev.TestEvent", handler, "EVENT.TestEvent")
        event = listener._message_factory.build_event("Ev.TestEvent", {"message": "test"}, "EVENT.TestEvent")
        await listener._handler(event, "cid", {}, routed("EVENT.TestEvent"))
        assert calls == [({"message": "test"}, "Ev.TestEvent", "EVENT.TestEvent")]
        assert conn.bindings[-1] == {"queue": "test-queue", "exchange": Config.events_exchange_name(), "routing_key": "EVENT.TestEvent"}

    async def test_calls_multiple_handlers_once_each(self):
        _, listener = await self.listener()
        calls = []

        async def h1(*_a):
            calls.append("h1")

        async def h2(*_a):
            calls.append("h2")

        await listener.subscribe("Ev.TestEvent", h1)
        await listener.subscribe("Ev.TestEvent", h2)
        event = listener._message_factory.build_event("Ev.TestEvent", {"message": "x"}, "EVENT.Ev.TestEvent")
        await listener._handler(event, "cid", {}, routed("EVENT.Ev.TestEvent"))
        assert sorted(calls) == ["h1", "h2"]

    async def test_routes_by_the_broker_routing_key_not_the_body_topic(self):
        _, listener = await self.listener()
        calls = []

        async def admin_handler(*_a):
            calls.append("admin")

        async def public_handler(*_a):
            calls.append("public")

        await listener.subscribe("Ev.TestEvent", admin_handler, "EVENT.admin")
        await listener.subscribe("Ev.TestEvent", public_handler, "EVENT.public")
        # A publisher permitted only to reach EVENT.public claims EVENT.admin in the body.
        forged = listener._message_factory.build_event("Ev.TestEvent", {"message": "x"}, "EVENT.admin")
        await listener._handler(forged, "cid", {}, routed("EVENT.public"))
        assert calls == ["public"]

    async def test_falls_back_to_the_body_topic_without_a_routing_key(self):
        _, listener = await self.listener()
        calls = []

        async def handler(*_a):
            calls.append(1)

        await listener.subscribe("Ev.TestEvent", handler)
        event = listener._message_factory.build_event("Ev.TestEvent", {"message": "x"}, "EVENT.Ev.TestEvent")
        await listener._handler(event, "cid", {})
        assert calls == [1]

    async def test_supports_one_and_two_argument_handlers(self):
        _, listener = await self.listener()
        calls = []

        async def one(data):
            calls.append(("one", data))

        async def two(data, topic):
            calls.append(("two", data, topic))

        await listener.subscribe("Ev.TestEvent", one)
        await listener.subscribe("Ev.TestEvent", two)
        event = listener._message_factory.build_event("Ev.TestEvent", {"message": "x"}, "EVENT.Ev.TestEvent")
        await listener._handler(event, "cid", {}, routed("EVENT.Ev.TestEvent"))
        assert sorted(calls, key=str) == [("one", {"message": "x"}), ("two", {"message": "x"}, "EVENT.Ev.TestEvent")]

    async def test_subscribe_all_hears_everything(self):
        conn, listener = await self.listener()
        seen = []

        async def catch_all(data, event_type, topic):
            seen.append((event_type, topic))

        await listener.subscribe_all(catch_all)
        assert conn.bindings[-1]["routing_key"] == "#"
        event = listener._message_factory.build_event("Ev.Other", {"n": 1}, "EVENT.Ev.Other")
        await listener._handler(event, "cid", {}, routed("EVENT.Ev.Other"))
        assert seen == [("Ev.Other", "EVENT.Ev.Other")]

    async def test_wildcard_subscriptions_match(self):
        _, listener = await self.listener()
        calls = []

        async def handler(data, event_type, topic):
            calls.append(topic)

        await listener.subscribe("Ev.TestEvent", handler, "EVENT.orders.*")
        event = listener._message_factory.build_event("Ev.TestEvent", {}, "EVENT.orders.created")
        await listener._handler(event, "cid", {}, routed("EVENT.orders.created"))
        await listener._handler(event, "cid", {}, routed("EVENT.orders.a.b"))
        assert calls == ["EVENT.orders.created"]

    async def test_a_handler_failure_propagates_so_the_delivery_is_settled_as_failed(self):
        _, listener = await self.listener()

        async def handler(*_a):
            raise RuntimeError("boom")

        await listener.subscribe("Ev.TestEvent", handler)
        event = listener._message_factory.build_event("Ev.TestEvent", {}, "EVENT.Ev.TestEvent")
        with pytest.raises(RuntimeError):
            await listener._handler(event, "cid", {}, routed("EVENT.Ev.TestEvent"))

    async def test_is_a_late_ack_consumer_with_bounded_prefetch(self):
        conn, listener = await self.listener()
        await listener.start()
        assert conn.consumes[-1]["late_ack"] is True
        assert conn.prefetches == [Config.default_prefetch()]
        assert conn.consumes[-1]["retry_options"] is None

    async def test_bindings_are_restored_after_reconnection(self):
        conn, listener = await self.listener()

        async def handler(*_a):
            pass

        await listener.subscribe("Ev.TestEvent", handler, "EVENT.a")
        await listener.subscribe("Ev.TestEvent", handler, "EVENT.b")
        await listener.start()
        before = len(conn.bindings)
        await conn.reconnect_now()
        rebound = [b["routing_key"] for b in conn.bindings[before:]]
        assert rebound == ["EVENT.a", "EVENT.b"]
        assert len(conn.consumes) == 2


class TestEventRetryTopology:
    async def test_declares_nothing_extra_by_default(self):
        conn = FakeConnection()
        listener = EventListener(conn, make_factory(EVENT_PROTO))
        await listener.init(None, "Svc.Events")
        assert [q["queue"] for q in conn.declared_queues] == ["Svc.Events"]
        assert listener.retry_topology is None
        assert listener.get_retry_options() is None

    async def test_declares_the_ladder_when_enabled(self):
        conn = FakeConnection()
        listener = EventListener(conn, make_factory(EVENT_PROTO), EventRetryOptions(max_retries=2, retry_delay_ms=250))
        await listener.init(None, "Svc.Events")
        queues = {q["queue"]: q["options"] for q in conn.declared_queues}
        assert set(queues) == {"Svc.Events", "Svc.Events.DLQ", "Svc.Events.Retry"}
        assert queues["Svc.Events.Retry"]["arguments"] == {"x-message-ttl": 250, "x-dead-letter-exchange": "Svc.Events.Redelivery"}
        exchanges = {e["exchange"] for e in conn.declared_exchanges}
        assert {"Svc.Events.Redelivery", "Svc.Events.Retry.Exchange"} <= exchanges
        # The expired event comes back through a per-subscriber exchange bound
        # only to this listener's own queue, never through the fan-out exchange.
        assert {"queue": "Svc.Events", "exchange": "Svc.Events.Redelivery", "routing_key": "#"} in conn.bindings
        assert {"queue": "Svc.Events.Retry", "exchange": "Svc.Events.Retry.Exchange", "routing_key": "#"} in conn.bindings
        assert listener.retry_topology == {"retry_queue": "Svc.Events.Retry", "dlq": "Svc.Events.DLQ"}
        opts = listener.get_retry_options()
        assert (opts.max_retries, opts.retry_queue_name, opts.retry_exchange_name, opts.dlq_name) == (2, "Svc.Events.Retry", "Svc.Events.Retry.Exchange", "Svc.Events.DLQ")

    async def test_the_retry_options_reach_the_consumer(self):
        conn = FakeConnection()
        listener = EventListener(conn, make_factory(EVENT_PROTO), EventRetryOptions(max_retries=1))
        await listener.init(None, "Svc.Events")
        await listener.start()
        assert conn.consumes[-1]["retry_options"].dlq_name == "Svc.Events.DLQ"

    async def test_skipped_for_an_anonymous_queue(self):
        conn = FakeConnection()
        listener = EventListener(conn, make_factory(EVENT_PROTO), EventRetryOptions(max_retries=1))
        await listener.init(None, "")
        assert listener.retry_topology is None

    async def test_a_changed_delay_is_reported_as_a_mismatch(self):
        from protobus import RetryQueueMismatchError

        conn = FakeConnection()
        original = conn.declare_queue

        async def declare(channel, name, options=None):
            if name.endswith(".Retry"):
                raise RuntimeError("PRECONDITION_FAILED - inequivalent arg 'x-message-ttl'")
            return await original(channel, name, options)

        conn.declare_queue = declare  # type: ignore[method-assign]
        listener = EventListener(conn, make_factory(EVENT_PROTO), EventRetryOptions(max_retries=1))
        with pytest.raises(RetryQueueMismatchError, match="retry_delay_ms"):
            await listener.init(None, "Svc.Events")


class TestTrie:
    def test_exact_simple_match(self):
        trie = Trie()
        trie.add("a.b.c", "abc")
        trie.add("b.c.d", 2)
        assert trie.match("a.b.c") == ["abc"]
        assert trie.match("b.c.d") == [2]
        assert trie.match("c.d.e") == []

    def test_a_node_split(self):
        trie = Trie()
        trie.add("a.b.c.2", 2)
        trie.add("a.b.c.1", 1)
        assert trie.match("a.b.c.1") == [1]
        assert trie.match("a.b.c.2") == [2]

    def test_does_not_return_a_match_if_not_a_leaf(self):
        trie = Trie()
        trie.add("a.b.c.d", "something")
        for topic in ("a", "a.b", "a.b.c"):
            assert trie.match(topic) == []
        assert trie.match("a.b.c.d") == ["something"]

    def test_star_wildcard_in_all_positions(self):
        trie = Trie()
        trie.add("*.b.c", "first")
        trie.add("a.*.c", "second")
        trie.add("a.b.*", "third")
        assert sorted(trie.match("a.b.c")) == ["first", "second", "third"]
        assert trie.match("z.b.c") == ["first"]
        assert trie.match("a.z.c") == ["second"]
        assert trie.match("a.b.z") == ["third"]

    def test_hash_super_wildcard_replacing_zero_or_more_words(self):
        trie = Trie()
        trie.add("#.b.c", "first")
        trie.add("a.#.c", "second")
        trie.add("a.b.#", "third")
        for topic in ("z.b.c", "x.z.b.c", "x.y.z.b.c", "b.c"):
            assert trie.match(topic) == ["first"], topic
        assert trie.match("b.b.b") == [] and trie.match("c.c.c") == []
        for topic in ("a.z.c", "a.x.z.c", "a.x.y.z.c", "a.c"):
            assert trie.match(topic) == ["second"], topic
        assert trie.match("a.a.a") == []
        for topic in ("a.b.z", "a.b.x.z", "a.b.x.y.z", "a.b"):
            assert trie.match(topic) == ["third"], topic

    def test_rabbit_blog_post_cases(self):
        trie = Trie()
        trie.add("a.b.c", "first")
        trie.add("a.*.b.c", "second")
        trie.add("a.#.c", "third")
        trie.add("b.b.c", "forth")
        assert trie.match("a.d.d.d.c") == ["third"]

    def test_rabbitmq_topics_tutorial(self):
        trie = Trie()
        trie.add("*.orange.*", "Q1")
        trie.add("*.*.rabbit", "Q2")
        trie.add("lazy.#", "Q2")
        assert len(trie.match("quick.orange.rabbit")) == 2
        assert len(trie.match("lazy.orange.elephant")) == 2
        assert trie.match("quick.orange.fox") == ["Q1"]
        assert trie.match("lazy.brown.fox") == ["Q2"]
        assert trie.match("lazy.pink.rabbit") == ["Q2"]
        assert trie.match("orange") == []
        assert trie.match("quick.brown.fox") == []
        assert trie.match("lazy.orange.male.rabbit") == ["Q2"]

    def test_keeps_both_handlers_registered_on_the_same_topic(self):
        trie = Trie()
        trie.add("EVENT.Order", "a")
        trie.add("EVENT.Order", "b")
        assert sorted(trie.match("EVENT.Order")) == ["a", "b"]

    def test_keeps_a_pattern_that_a_longer_one_is_later_added_beneath(self):
        for order in (("EVENT.Order", "EVENT.Order.Shipped"), ("EVENT.Order.Shipped", "EVENT.Order")):
            trie = Trie()
            for pattern in order:
                trie.add(pattern, "long" if pattern.endswith("Shipped") else "short")
            assert trie.match("EVENT.Order") == ["short"]
            assert trie.match("EVENT.Order.Shipped") == ["long"]

    def test_keeps_intermediate_values_out_of_unrelated_matches(self):
        trie = Trie()
        trie.add("EVENT.Order", "short")
        trie.add("EVENT.Order.Shipped", "long")
        trie.add("EVENT.Invoice.Paid", "other")
        assert trie.match("EVENT.Invoice") == []
        assert trie.match("EVENT.Invoice.Paid") == ["other"]

    def test_keeps_several_handlers_under_a_wildcard(self):
        trie = Trie()
        trie.add("EVENT.*", "x")
        trie.add("EVENT.*", "y")
        trie.add("EVENT.Order", "exact")
        assert sorted(trie.match("EVENT.Order")) == ["exact", "x", "y"]

    @pytest.mark.parametrize("event, expected", [
        ("ORDERS.US.CREATED", ["A", "B"]),
        ("ORDERS.US.123.CREATED", ["B"]),
        ("ORDERS.US.123.SHIPPED", ["B", "C"]),
        ("ORDERS.EU.456.SHIPPED", ["B"]),
        ("ORDERS", ["B"]),
    ])
    def test_the_wildcard_example_in_the_docs(self, event, expected):
        trie = Trie()
        trie.add("ORDERS.*.CREATED", "A")
        trie.add("ORDERS.#", "B")
        trie.add("ORDERS.US.*.SHIPPED", "C")
        assert sorted(trie.match(event)) == expected


class TestConfigNumericEnvParsing:
    def test_uses_the_default_when_unset(self):
        assert Config.message_processing_timeout() == 600000
        assert Config.stream_idle_timeout_ms() == 60000

    def test_honours_a_valid_override(self, monkeypatch):
        monkeypatch.setenv("MESSAGE_PROCESSING_TIMEOUT", "1234")
        assert Config.message_processing_timeout() == 1234

    @pytest.mark.parametrize("raw", ["6oo000", "   ", "0", "-5", "123abc", "1.5"])
    def test_falls_back_to_the_default_on_a_malformed_value(self, monkeypatch, raw):
        monkeypatch.setenv("MESSAGE_PROCESSING_TIMEOUT", raw)
        assert Config.message_processing_timeout() == 600000

    def test_re_reads_a_changed_value(self, monkeypatch):
        monkeypatch.setenv("MESSAGE_PROCESSING_TIMEOUT", "10")
        assert Config.message_processing_timeout() == 10
        monkeypatch.setenv("MESSAGE_PROCESSING_TIMEOUT", "20")
        assert Config.message_processing_timeout() == 20

    def test_exposes_a_unary_rpc_call_timeout_with_a_sane_default(self):
        assert Config.rpc_call_timeout_ms() == 600000

    def test_boolean_parsing(self, monkeypatch):
        assert Config.expose_internal_errors() is True
        for raw in ("0", "false", "no", "off", "FALSE"):
            monkeypatch.setenv("PROTOBUS_EXPOSE_INTERNAL_ERRORS", raw)
            assert Config.expose_internal_errors() is False
        for raw in ("1", "true", "yes", "on"):
            monkeypatch.setenv("PROTOBUS_EXPOSE_INTERNAL_ERRORS", raw)
            assert Config.expose_internal_errors() is True
        monkeypatch.setenv("PROTOBUS_EXPOSE_INTERNAL_ERRORS", "maybe")
        assert Config.expose_internal_errors() is True

    def test_exchange_names_and_constants(self, monkeypatch):
        assert Config.bus_exchange_name() == "proto.bus"
        assert Config.callbacks_exchange_name() == "proto.bus.callback"
        assert Config.events_exchange_name() == "proto.bus.events"
        assert Config.cancel_exchange_name() == "proto.bus.cancel"
        monkeypatch.setenv("BUS_EXCHANGE_NAME", "custom.bus")
        assert Config.bus_exchange_name() == "custom.bus"
        assert (Config.PRIORITY_NORMAL, Config.PRIORITY_HIGH, Config.PRIORITY_CONTROL, Config.RECOMMENDED_MAX_PRIORITY) == (0, 1, 2, 2)
        assert (Config.HEADER_FINAL, Config.HEADER_SEQ) == ("x-protobus-final", "x-protobus-seq")

    def test_defaults_match_the_typescript_port(self):
        assert Config.default_prefetch() == 1
        assert Config.publish_confirm_timeout_ms() == 30000
        assert Config.connection_ready_timeout_ms() == 30000
        assert Config.max_outstanding_confirms() == 256
        assert Config.stream_max_buffered_chunks() == 1024
        assert Config.stream_max_buffered_bytes() == 64 * 1024 * 1024
        assert Config.stream_max_total_buffered_bytes() == 256 * 1024 * 1024
