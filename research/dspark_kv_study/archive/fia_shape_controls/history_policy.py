"""CPU reference policy for query-aware historical KV selection.

This module does not modify a vLLM block table or implement a serving backend.
Positions are absolute positions of already cached tokens, before draft queries.
"""

import math
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class Selection:
    positions: tuple[int, ...]
    anchor_blocks: tuple[int, ...]
    local_start: int


class BM25HistoryIndex:
    """Index token-ID chunks once per request; rank only eligible history."""

    def __init__(self, token_ids, block_tokens=128, k1=1.5, b=0.75):
        if block_tokens <= 0 or k1 <= 0 or not 0 <= b <= 1:
            raise ValueError("invalid BM25 parameters")
        self.token_count = len(token_ids)
        self.block_tokens = block_tokens
        self.k1 = k1
        self.b = b
        self.documents = tuple(
            Counter(token_ids[start : start + block_tokens]) for start in range(0, len(token_ids), block_tokens)
        )
        self.lengths = tuple(sum(document.values()) for document in self.documents)
        self.document_frequency = Counter()
        for document in self.documents:
            self.document_frequency.update(document.keys())
        self.average_length = self.token_count / max(1, len(self.documents))

    def rank(self, query_ids, eligible_blocks):
        query_terms = set(query_ids)
        document_count = len(self.documents)
        scores = []
        for block_id in eligible_blocks:
            document = self.documents[block_id]
            normalization = self.k1 * (1 - self.b + self.b * self.lengths[block_id] / max(1, self.average_length))
            score = 0.0
            for term in query_terms:
                frequency = document.get(term, 0)
                if frequency:
                    df = self.document_frequency[term]
                    inverse_frequency = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
                    score += inverse_frequency * frequency * (self.k1 + 1) / (frequency + normalization)
            scores.append((score, block_id))
        # Zero-score ties prefer recent history; sorting makes selection deterministic.
        return tuple(block_id for _, block_id in sorted(scores, key=lambda pair: (-pair[0], -pair[1])))


def select_positions(index, query_ids, context_length, local_tokens=2048, anchor_tokens=128):
    """Return at most local_tokens + anchor_tokens exact cached-token positions.

    Only complete, non-overlapping historical chunks are anchors. Short context
    lengths and partial final chunks never introduce padding or future tokens.
    The retrieval index may cover the original prompt while context_length grows.
    """
    if context_length < 0 or local_tokens <= 0 or anchor_tokens < 0:
        raise ValueError("invalid context or budget")
    if anchor_tokens % index.block_tokens:
        raise ValueError("anchor budget must contain complete retrieval chunks")
    local_start = max(0, context_length - local_tokens)
    eligible_count = min(local_start, index.token_count) // index.block_tokens
    ranked = index.rank(query_ids, range(eligible_count))
    anchors = tuple(sorted(ranked[: anchor_tokens // index.block_tokens]))
    history = tuple(
        position
        for block in anchors
        for position in range(block * index.block_tokens, (block + 1) * index.block_tokens)
    )
    return Selection(history + tuple(range(local_start, context_length)), anchors, local_start)


def physical_addresses(selection, full_block_table, kernel_block_tokens):
    """Map exact logical positions to (physical page, offset), without compaction.

    A page number alone is insufficient when selections cut through pages.
    This reference deliberately retains offsets instead of silently rounding.
    """
    if kernel_block_tokens <= 0:
        raise ValueError("kernel block size must be positive")
    addresses = []
    for position in selection.positions:
        page, offset = divmod(position, kernel_block_tokens)
        if page >= len(full_block_table):
            raise ValueError("block table does not cover selected absolute positions")
        addresses.append((full_block_table[page], offset))
    return tuple(addresses)
