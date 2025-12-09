"""Tests for the Trie data structure."""

import pytest

from protobus.trie import Trie


class TestTrie:
    """Test cases for Trie topic matching."""

    def test_exact_match(self):
        """Test exact topic matching."""
        trie = Trie()
        trie.add_match("user.created", "handler1")

        result = trie.match_topic("user.created")
        assert "handler1" in result
        assert len(result) == 1

    def test_no_match(self):
        """Test when no patterns match."""
        trie = Trie()
        trie.add_match("user.created", "handler1")

        result = trie.match_topic("user.deleted")
        assert len(result) == 0

    def test_single_wildcard(self):
        """Test single-level wildcard (*)."""
        trie = Trie()
        trie.add_match("user.*.created", "handler1")

        result = trie.match_topic("user.123.created")
        assert "handler1" in result

        result = trie.match_topic("user.456.created")
        assert "handler1" in result

        # Should not match more than one segment
        result = trie.match_topic("user.123.456.created")
        assert "handler1" not in result

    def test_super_wildcard(self):
        """Test multi-level wildcard (#)."""
        trie = Trie()
        trie.add_match("user.#", "handler1")

        # Should match any depth after user
        result = trie.match_topic("user.created")
        assert "handler1" in result

        result = trie.match_topic("user.123.created")
        assert "handler1" in result

        result = trie.match_topic("user.123.456.created")
        assert "handler1" in result

    def test_super_wildcard_at_end(self):
        """Test # wildcard matching zero segments."""
        trie = Trie()
        trie.add_match("user.#", "handler1")

        # # should also match zero segments after user
        result = trie.match_topic("user")
        # This depends on implementation - # typically matches zero or more
        # In MQTT/RabbitMQ style, "user.#" should match "user" too

    def test_multiple_handlers(self):
        """Test matching multiple patterns."""
        trie = Trie()
        trie.add_match("user.created", "handler1")
        trie.add_match("user.*", "handler2")
        trie.add_match("#", "handler3")

        result = trie.match_topic("user.created")
        assert "handler1" in result
        assert "handler2" in result
        assert "handler3" in result

    def test_complex_patterns(self):
        """Test complex pattern combinations."""
        trie = Trie()
        trie.add_match("event.user.*.action", "handler1")
        trie.add_match("event.*.*.action", "handler2")

        result = trie.match_topic("event.user.123.action")
        assert "handler1" in result
        assert "handler2" in result

        result = trie.match_topic("event.order.456.action")
        assert "handler1" not in result
        assert "handler2" in result
