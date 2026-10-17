"""Semantic checks independent of torch, vLLM, model weights and NPU hardware."""

import random
import unittest

from history_policy import BM25HistoryIndex, physical_addresses, select_positions


class HistoryPolicyTests(unittest.TestCase):
    def test_retrieves_distant_question_term(self):
        tokens = [1] * 4096
        tokens[256:384] = [999] * 128
        selected = select_positions(BM25HistoryIndex(tokens), [999], len(tokens))
        self.assertEqual(selected.anchor_blocks, (2,))
        self.assertEqual(selected.positions, tuple(range(256, 384)) + tuple(range(2048, 4096)))

    def test_short_context_has_no_padding_or_fake_anchors(self):
        for length in (0, 1, 127, 128, 2047, 2048):
            selected = select_positions(BM25HistoryIndex([1] * length), [1], length)
            self.assertEqual(selected.positions, tuple(range(length)))

    def test_positions_are_unique_ordered_bounded_and_budgeted(self):
        rng = random.Random(0)
        for length in (2049, 2175, 2176, 2177, 2303, 2304, 4097, 16383, 24576):
            index = BM25HistoryIndex([rng.randrange(50) for _ in range(length)])
            selected = select_positions(index, [3, 5], length)
            self.assertEqual(selected.positions, tuple(sorted(set(selected.positions))))
            self.assertLessEqual(len(selected.positions), 2176)
            self.assertTrue(all(0 <= position < length for position in selected.positions))
            self.assertEqual(selected.positions[-2048:], tuple(range(length - 2048, length)))

    def test_partial_page_mapping_preserves_absolute_addresses(self):
        length = 4097
        tokens = [1] * length
        tokens[128:256] = [99] * 128
        selected = select_positions(BM25HistoryIndex(tokens), [99], length)
        for page_tokens in (16, 64, 128, 256):
            table = list(reversed(range((length + page_tokens - 1) // page_tokens)))
            inverse = {physical: logical for logical, physical in enumerate(table)}
            addresses = physical_addresses(selected, table, page_tokens)
            reconstructed = tuple(inverse[page] * page_tokens + offset for page, offset in addresses)
            self.assertEqual(reconstructed, selected.positions)

    def test_cached_query_ranking_is_repeatable(self):
        index = BM25HistoryIndex(list(range(4096)))
        first = select_positions(index, [9, 10, 10], 4096)
        self.assertEqual(first, select_positions(index, [10, 9], 4096))

    def test_growing_context_keeps_cached_prompt_index_valid(self):
        index = BM25HistoryIndex([7] * 4096)
        selected = select_positions(index, [7], 4608)
        self.assertTrue(all(block * 128 < 4096 for block in selected.anchor_blocks))
        self.assertEqual(selected.positions[-2048:], tuple(range(2560, 4608)))

    def test_bad_budget_and_incomplete_table_fail(self):
        index = BM25HistoryIndex([1] * 4096)
        for kwargs in ({"anchor_tokens": 127}, {"local_tokens": 0}, {"anchor_tokens": -128}):
            with self.assertRaises(ValueError):
                select_positions(index, [1], 4096, **kwargs)
        with self.assertRaises(ValueError):
            physical_addresses(select_positions(index, [1], 4096), [0], 128)


if __name__ == "__main__":
    unittest.main()
