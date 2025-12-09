"""Trie data structure for topic matching with wildcard support."""

from typing import Any, Dict, List, Optional, Set


class TrieNode:
    """
    A node in the Trie data structure supporting wildcard matching.

    Supports two wildcard types:
    - '*' (single-level): matches exactly one segment
    - '#' (multi-level): matches zero or more segments
    """

    def __init__(self, word: str = ""):
        self.word = word
        self.value: Optional[Any] = None
        self.children: Dict[str, "TrieNode"] = {}
        self.single_wildcard: Optional["TrieNode"] = None  # '*' wildcard
        self.super_wildcard: Optional["TrieNode"] = None   # '#' wildcard

    def add_match(self, pattern: str, value: Any) -> None:
        """
        Add a pattern to the trie with an associated value.

        Args:
            pattern: Dot-separated pattern string (e.g., "user.*.created")
            value: Value to associate with the pattern
        """
        parts = pattern.split(".")
        self._add_match_deep(parts, 0, value)

    def _add_match_deep(self, parts: List[str], index: int, value: Any) -> None:
        """Recursively add pattern parts to the trie."""
        if index >= len(parts):
            self.value = value
            return

        part = parts[index]

        if part == "*":
            if self.single_wildcard is None:
                self.single_wildcard = TrieNode("*")
            self.single_wildcard._add_match_deep(parts, index + 1, value)
        elif part == "#":
            if self.super_wildcard is None:
                self.super_wildcard = TrieNode("#")
            self.super_wildcard._add_match_deep(parts, index + 1, value)
        else:
            if part not in self.children:
                self.children[part] = TrieNode(part)
            self.children[part]._add_match_deep(parts, index + 1, value)

    def match_topic(self, topic: str) -> List[Any]:
        """
        Find all values matching a given topic string.

        Args:
            topic: Dot-separated topic string (e.g., "user.123.created")

        Returns:
            List of values from matching patterns
        """
        parts = topic.split(".")
        results: Set[Any] = set()
        self._match_topic_deep(parts, 0, results)
        return list(results)

    def _match_topic_deep(self, parts: List[str], index: int, results: Set[Any]) -> None:
        """Recursively match topic parts against the trie."""
        # If we've consumed all parts, check for a value at this node
        if index >= len(parts):
            if self.value is not None:
                results.add(self.value)
            # Also check super wildcard at this level (# can match zero segments)
            if self.super_wildcard is not None and self.super_wildcard.value is not None:
                results.add(self.super_wildcard.value)
            return

        current_part = parts[index]

        # Check exact match
        if current_part in self.children:
            self.children[current_part]._match_topic_deep(parts, index + 1, results)

        # Check single wildcard (*)
        if self.single_wildcard is not None:
            self.single_wildcard._match_topic_deep(parts, index + 1, results)

        # Check super wildcard (#)
        if self.super_wildcard is not None:
            # # can match zero or more segments
            # Try matching zero segments (skip to value check)
            if self.super_wildcard.value is not None:
                results.add(self.super_wildcard.value)

            # Try matching one segment
            self.super_wildcard._match_topic_deep(parts, index + 1, results)

            # Try matching multiple segments by staying at super wildcard
            for i in range(index + 1, len(parts)):
                self.super_wildcard._match_topic_deep(parts, i + 1, results)


class Trie:
    """Trie data structure for efficient topic matching."""

    def __init__(self):
        self._root = TrieNode()

    def add_match(self, pattern: str, value: Any) -> None:
        """
        Add a pattern to the trie with an associated value.

        Args:
            pattern: Dot-separated pattern string with optional wildcards
            value: Value to associate with the pattern
        """
        self._root.add_match(pattern, value)

    def match_topic(self, topic: str) -> List[Any]:
        """
        Find all values matching a given topic string.

        Args:
            topic: Dot-separated topic string

        Returns:
            List of values from matching patterns
        """
        return self._root.match_topic(topic)
