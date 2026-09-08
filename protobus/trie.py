"""Topic matching with RabbitMQ's wildcards: ``*`` for exactly one word,
``#`` for zero or more."""

from typing import Any, Dict, List, Optional, Set


class TrieNode:
    def __init__(self, word: str = "") -> None:
        self.word = word
        # A list, not a single slot: several subscribers may register the
        # same pattern. Empty on a node that is only a step along the way to
        # a longer pattern, which is what keeps partial matches from matching.
        self.values: List[Any] = []
        self.children: Dict[str, "TrieNode"] = {}
        self.single_wildcard: Optional["TrieNode"] = None  # '*'
        self.super_wildcard: Optional["TrieNode"] = None  # '#'

    def add_match(self, pattern: str, value: Any) -> None:
        node = self
        for part in pattern.split("."):
            if part == "*":
                if node.single_wildcard is None:
                    node.single_wildcard = TrieNode("*")
                node = node.single_wildcard
            elif part == "#":
                if node.super_wildcard is None:
                    node.super_wildcard = TrieNode("#")
                node = node.super_wildcard
            else:
                node = node.children.setdefault(part, TrieNode(part))
        node.values.append(value)

    def match_topic(self, topic: str) -> List[Any]:
        results: List[Any] = []
        seen: Set[int] = set()
        self._match(topic.split("."), 0, results, seen)
        return results

    def _collect(self, results: List[Any], seen: Set[int]) -> None:
        for value in self.values:
            # Deduplicate one value reached through two patterns, keeping
            # registration order.
            if id(value) not in seen:
                seen.add(id(value))
                results.append(value)

    def _match(self, parts: List[str], index: int, results: List[Any], seen: Set[int]) -> None:
        if index == len(parts):
            self._collect(results, seen)
            # A trailing '#' matches zero words, so a pattern ending in one
            # ends here too.
            if self.super_wildcard is not None:
                self.super_wildcard._match(parts, index, results, seen)
            return

        part = parts[index]
        child = self.children.get(part)
        if child is not None:
            child._match(parts, index + 1, results, seen)
        if self.single_wildcard is not None:
            self.single_wildcard._match(parts, index + 1, results, seen)
        if self.super_wildcard is not None:
            # '#' stands for zero or more words: continue from the '#' node
            # having consumed none, one, ... all of the remaining words.
            for consumed in range(index, len(parts) + 1):
                self.super_wildcard._match(parts, consumed, results, seen)


class Trie:
    """Maps topic patterns to values; ``match`` returns every value whose
    pattern matches the topic, each at most once."""

    def __init__(self) -> None:
        self._root = TrieNode()

    def add_match(self, pattern: str, value: Any) -> None:
        self._root.add_match(pattern, value)

    def match_topic(self, topic: str) -> List[Any]:
        return self._root.match_topic(topic)

    # TS parity names.
    add = add_match
    match = match_topic
