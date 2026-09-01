"""
Every pattern registered on a node must survive.

Parity with TS protobus 2461a28 ("fix(trie): keep every pattern registered on
a node"). A node held a single ``value`` slot and ``_add_match_deep`` assigned
straight into it, so a second subscriber to the same topic silently replaced
the first: the binding stayed in place, the broker kept delivering, and the
first handler simply stopped being called.
"""

from protobus.trie import Trie


class TestMultipleHandlersPerPattern:
    def test_two_handlers_on_the_same_pattern_both_match(self):
        trie = Trie()

        def first(*_):
            return "first"

        def second(*_):
            return "second"

        trie.add_match("EVENT.Order.Created", first)
        trie.add_match("EVENT.Order.Created", second)

        matched = trie.match_topic("EVENT.Order.Created")

        assert set(matched) == {first, second}, (
            "a second subscriber to the same topic was discarded"
        )

    def test_two_handlers_on_the_same_wildcard_pattern_both_match(self):
        trie = Trie()

        def first(*_):
            return "first"

        def second(*_):
            return "second"

        trie.add_match("EVENT.Order.*", first)
        trie.add_match("EVENT.Order.*", second)

        assert set(trie.match_topic("EVENT.Order.Shipped")) == {first, second}

    def test_two_handlers_on_the_same_super_wildcard_both_match(self):
        trie = Trie()

        def first(*_):
            return "first"

        def second(*_):
            return "second"

        trie.add_match("EVENT.#", first)
        trie.add_match("EVENT.#", second)

        assert set(trie.match_topic("EVENT.Order.Shipped")) == {first, second}


class TestExistingBehaviourPreserved:
    """Guards the properties the fix must not regress."""

    def test_a_deeper_pattern_does_not_hide_a_shallower_one(self):
        trie = Trie()

        def shallow(*_):
            return "shallow"

        def deep(*_):
            return "deep"

        trie.add_match("EVENT.Order", shallow)
        trie.add_match("EVENT.Order.Shipped", deep)

        assert trie.match_topic("EVENT.Order") == [shallow]
        assert trie.match_topic("EVENT.Order.Shipped") == [deep]

    def test_distinct_patterns_matching_one_topic_all_fire(self):
        trie = Trie()

        def exact(*_):
            return "exact"

        def single(*_):
            return "single"

        def multi(*_):
            return "multi"

        trie.add_match("EVENT.Order.Created", exact)
        trie.add_match("EVENT.Order.*", single)
        trie.add_match("EVENT.#", multi)

        assert set(trie.match_topic("EVENT.Order.Created")) == {exact, single, multi}

    def test_no_match_returns_empty(self):
        trie = Trie()
        trie.add_match("EVENT.Order.Created", lambda *_: None)
        assert trie.match_topic("EVENT.Payment.Created") == []

    def test_the_same_handler_registered_twice_is_reported_once(self):
        trie = Trie()

        def handler(*_):
            return "h"

        trie.add_match("EVENT.Order.Created", handler)
        trie.add_match("EVENT.Order.*", handler)

        assert trie.match_topic("EVENT.Order.Created") == [handler]
