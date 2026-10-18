import copy
import unittest

from audit_draft_reads import check_record


def example():
    full = list(range(100, 132))
    positions = list(range(4089, 4096))
    return {
        "gid": 2,
        "layer": "draft.1",
        "query_per_request": 7,
        "block_size": 128,
        "window_config": 2176,
        "kv_lengths": [2176],
        "request_ids": ["request-0"],
        "full_block_tables": [full],
        "read_block_tables": [full[15:]],
        "query_positions": positions,
        "query_slot_mapping": [full[p // 128] * 128 + p % 128 for p in positions],
    }


class DraftReadAuditTest(unittest.TestCase):
    def test_valid_window_and_full(self):
        record = example()
        self.assertTrue(check_record(record)[0]["passed"])
        record["window_config"] = None
        record["kv_lengths"] = [4096]
        record["read_block_tables"] = record["full_block_tables"]
        self.assertTrue(check_record(record)[0]["passed"])

    def test_stock_bug_restoring_full_table_is_detected(self):
        record = example()
        record["read_block_tables"] = record["full_block_tables"]
        row = check_record(record)[0]
        self.assertFalse(row["read_positions_correct"])
        self.assertTrue(row["absolute_writes_preserved"])

    def test_read_side_and_write_side_are_checked_independently(self):
        record = copy.deepcopy(example())
        record["query_slot_mapping"][0] += 128
        row = check_record(record)[0]
        self.assertTrue(row["read_positions_correct"])
        self.assertFalse(row["absolute_writes_preserved"])


if __name__ == "__main__":
    unittest.main()
