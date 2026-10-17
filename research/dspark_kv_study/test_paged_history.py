import copy
import unittest
from types import SimpleNamespace

import torch
from paged_history import HistoryConfig, PagedHistoryAdapter, begin_dspark_history, choose_anchor_pages


def request(prompt_length, page=128, relevant_page=3):
    tokens = [0] * prompt_length
    if prompt_length > (relevant_page + 1) * page:
        tokens[relevant_page * page : (relevant_page + 1) * page] = [999] * page
    return SimpleNamespace(
        prompt_token_ids=tokens,
        sampling_params=SimpleNamespace(extra_args={"history_query_token_ids": [999]}),
    )


def metadata(lengths, block_size, physical_offset=1000):
    rows = len(lengths)
    columns = max(1, (max(lengths) + block_size - 1) // block_size)
    # Physical pages deliberately have no correspondence with logical order.
    table = torch.arange(rows * columns, dtype=torch.int32).reshape(rows, columns).flip(1) + physical_offset
    exact = torch.tensor(lengths, dtype=torch.int32)
    return SimpleNamespace(
        num_reqs=rows,
        block_table_tensor=table,
        seq_lens=exact,
        seq_lens_cpu=exact + 7,
        _seq_lens_cpu=exact + 7,
        seq_lens_cpu_upper_bound=exact + 7,
        parallel_draft_seq_lens_cpu=exact + 14,
        parallel_draft_num_reject_cpu=torch.tensor([7] * rows),
        parallel_draft_num_reject_event=object(),
        max_seq_len=max(lengths),
        slot_mapping=torch.arange(rows * 7, dtype=torch.int32) + 10000,
        positions=torch.arange(rows * 7, dtype=torch.int64) + 20000,
        query_start_loc=torch.arange(rows + 1, dtype=torch.int32) * 7,
        causal=False,
    )


def read_absolute_positions(candidate, original, row, block_size):
    inverse = {int(physical): logical for logical, physical in enumerate(original.block_table_tensor[row])}
    return [
        inverse[int(candidate.block_table_tensor[row, index // block_size])] * block_size + index % block_size
        for index in range(int(candidate.seq_lens[row]))
    ]


class PagedHistoryTests(unittest.TestCase):
    def test_bm25_and_cheap_selection_controls(self):
        state = request(4096)
        for policy, expected in (("bm25", (3,)), ("sink", (0,)), ("uniform", (8,)), ("recent", (15,))):
            config = HistoryConfig(policy=policy)
            self.assertEqual(choose_anchor_pages(state.prompt_token_ids, [999], 128, config), expected)

    def test_physical_reads_cpu_mirrors_and_original_metadata(self):
        for page in (16, 32, 128):
            for lengths in ((0, 256, 513), (2047, 2048, 2049), (2176, 2300, 4099), (16384, 24583, 25087)):
                with self.subTest(page=page, lengths=lengths):
                    config = HistoryConfig(local_tokens=2048, anchor_tokens=128, policy="sink")
                    adapter = PagedHistoryAdapter(config, 4, "cpu")
                    states = {str(row): request(length, page) for row, length in enumerate(lengths)}
                    adapter.begin_step(states, list(states))
                    original = metadata(lengths, page)
                    original_table = original.block_table_tensor.clone()
                    original_cpu = original._seq_lens_cpu.clone()
                    candidate = copy.copy(original)
                    adapter.apply(candidate, 1, page)
                    for row, length in enumerate(lengths):
                        start = max(0, length - config.local_tokens) // page * page
                        initial_count = max(0, length - config.local_tokens) // page
                        anchors = range(min(config.anchor_tokens // page, initial_count, start // page))
                        expected = [
                            position for anchor in anchors for position in range(anchor * page, (anchor + 1) * page)
                        ]
                        expected += list(range(start, length))
                        self.assertEqual(read_absolute_positions(candidate, original, row, page), expected)
                    self.assertTrue(torch.equal(candidate.seq_lens, candidate._seq_lens_cpu))
                    self.assertTrue(torch.equal(candidate.seq_lens, candidate.parallel_draft_seq_lens_cpu))
                    self.assertIsNone(candidate.parallel_draft_num_reject_event)
                    for name in ("slot_mapping", "positions", "query_start_loc"):
                        self.assertIs(getattr(candidate, name), getattr(original, name))
                    self.assertTrue(torch.equal(original.block_table_tensor, original_table))
                    self.assertTrue(torch.equal(original._seq_lens_cpu, original_cpu))

    def test_partial_prefill_filters_not_yet_readable_anchors(self):
        adapter = PagedHistoryAdapter(HistoryConfig(), 2, "cpu")
        adapter.begin_step({"r": request(24576)}, ["r"])
        for length in (128, 2055, 2300, 24583):
            original = metadata([length], 128)
            candidate = copy.copy(original)
            adapter.apply(candidate, 1, 128)
            start = max(0, length - 2048) // 128 * 128
            expected = list(range(384, 512)) if start > 384 else []
            expected += list(range(start, length))
            self.assertEqual(read_absolute_positions(candidate, original, 0, 128), expected)
            adapter.begin_step(adapter.requests, adapter.request_ids)
        self.assertEqual(adapter.selection_builds, 1)

    def test_each_group_has_its_own_table_and_one_length_copy(self):
        adapter = PagedHistoryAdapter(HistoryConfig(), 4, "cpu")
        adapter.begin_step({"r": request(4096)}, ["r"])
        first = copy.copy(metadata([4103], 128, 1000))
        second = copy.copy(metadata([4103], 128, 5000))
        adapter.apply(first, 1, 128)
        snapshot = first.block_table_tensor.clone()
        adapter.apply(second, 2, 128)
        self.assertTrue(torch.equal(snapshot, first.block_table_tensor))
        self.assertNotEqual(first.block_table_tensor.data_ptr(), second.block_table_tensor.data_ptr())
        self.assertEqual(adapter.selection_builds, 1)
        self.assertEqual(adapter.cpu_length_copies, 1)

    def test_selection_cache_survives_growth_and_row_reordering(self):
        adapter = PagedHistoryAdapter(HistoryConfig(), 4, "cpu")
        states = {"a": request(4096), "b": request(4096, relevant_page=5)}
        table_pointer = None
        for ids, length in ((["a", "b"], 4103), (["a", "b"], 4111), (["b", "a"], 4239)):
            adapter.begin_step(states, ids)
            original = metadata([length, length], 128)
            candidate = copy.copy(original)
            adapter.apply(candidate, 1, 128)
            for row, identity in enumerate(ids):
                anchor = 3 if identity == "a" else 5
                self.assertEqual(
                    read_absolute_positions(candidate, original, row, 128)[:128],
                    list(range(anchor * 128, (anchor + 1) * 128)),
                )
            if table_pointer is not None:
                self.assertEqual(candidate.block_table_tensor.data_ptr(), table_pointer)
            table_pointer = candidate.block_table_tensor.data_ptr()
        self.assertEqual(adapter.selection_builds, 2)
        self.assertEqual(adapter.anchor_batch_uploads, 2)

    def test_cache_invalidates_new_request_query_and_retirement(self):
        adapter = PagedHistoryAdapter(HistoryConfig(), 2, "cpu")
        state = request(4096)
        for current in (state, request(4096, relevant_page=5)):
            adapter.begin_step({"r": current}, ["r"])
            adapter.apply(copy.copy(metadata([4103], 128)), 1, 128)
        current.sampling_params.extra_args["history_query_token_ids"] = [0]
        adapter.begin_step({"r": current}, ["r"])
        adapter.apply(copy.copy(metadata([4103], 128)), 1, 128)
        self.assertEqual(adapter.selection_builds, 3)
        adapter.begin_step({}, [])
        self.assertEqual(adapter.entries, {})

    def test_no_cache_ablation_rebuilds_selection(self):
        adapter = PagedHistoryAdapter(HistoryConfig(cache_anchors=False), 2, "cpu")
        state = request(4096)
        for _ in range(2):
            adapter.begin_step({"r": state}, ["r"])
            adapter.apply(copy.copy(metadata([4103], 128)), 1, 128)
        self.assertEqual(adapter.selection_builds, 2)
        self.assertEqual(adapter.anchor_batch_uploads, 2)

    def test_dummy_rows_and_equal_budget_window_control(self):
        adapter = PagedHistoryAdapter(HistoryConfig(local_tokens=2176, anchor_tokens=0), 2, "cpu")
        adapter.begin_step({}, [])
        original = metadata([24583], 128)
        candidate = copy.copy(original)
        adapter.apply(candidate, 1, 128)
        start = (24583 - 2176) // 128 * 128
        self.assertEqual(read_absolute_positions(candidate, original, 0, 128), list(range(start, 24583)))

    def test_invalid_budgets_missing_question_and_unsupported_causality(self):
        with self.assertRaises(ValueError):
            HistoryConfig(policy="answers")
        with self.assertRaises(ValueError):
            choose_anchor_pages([0] * 4096, [], 128, HistoryConfig())
        with self.assertRaises(ValueError):
            choose_anchor_pages([0] * 4096, [0], 256, HistoryConfig())
        adapter = PagedHistoryAdapter(HistoryConfig(), 2, "cpu")
        adapter.begin_step({"r": request(4096)}, ["r"])
        bad = metadata([4103], 128)
        bad.causal = True
        with self.assertRaises(ValueError):
            adapter.apply(bad, 1, 128)

    def test_overlay_hook_does_not_change_full_mode(self):
        proposer = SimpleNamespace(vllm_config=SimpleNamespace(additional_config={}), draft_window_size=None)
        self.assertIsNone(begin_dspark_history(proposer, None))


if __name__ == "__main__":
    unittest.main()
