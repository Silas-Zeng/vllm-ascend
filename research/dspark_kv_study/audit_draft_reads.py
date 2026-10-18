"""Check live FIA block tables against absolute query positions and KV writes."""

import argparse
import hashlib
import json
from pathlib import Path


def check_record(record):
    query_count = record["query_per_request"]
    block_size = record["block_size"]
    window = record["window_config"]
    checks = []
    for row, length in enumerate(record["kv_lengths"]):
        positions = record["query_positions"][row * query_count : (row + 1) * query_count]
        slots = record["query_slot_mapping"][row * query_count : (row + 1) * query_count]
        if len(positions) != query_count or len(slots) != query_count:
            raise ValueError("incomplete audited query block")
        end = max(positions) + 1
        start = max(0, end - window) // block_size * block_size if window else 0
        full = record["full_block_tables"][row]
        read = record["read_block_tables"][row]
        required_blocks = (length + block_size - 1) // block_size
        expected = full[start // block_size : start // block_size + required_blocks]
        read_correct = length == end - start and len(expected) == required_blocks and read[:required_blocks] == expected
        expected_slots = [full[position // block_size] * block_size + position % block_size for position in positions]
        checks.append(
            {
                "gid": record["gid"],
                "layer": record["layer"],
                "request_id": record["request_ids"][row],
                "full_end_position_exclusive": end,
                "expected_aligned_start": start,
                "observed_kv_length": length,
                "read_positions_correct": read_correct,
                "absolute_writes_preserved": slots == expected_slots,
                "passed": read_correct and slots == expected_slots,
            }
        )
    return checks


def summarize(directory):
    rows, sources = [], []
    for path in sorted(directory.glob("*_draft_reads.jsonl")):
        sources.append({"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        for line in path.read_text(encoding="utf-8").splitlines():
            rows.extend(check_record(json.loads(line)))
    if not rows:
        raise ValueError("no live draft FIA read records were captured")
    return {
        "scope": "live FIA read-side block table and absolute write mapping audit; excluded from timing",
        "source_files": sources,
        "checked_request_group_calls": len(rows),
        "group_ids": sorted({row["gid"] for row in rows}),
        "failed_reads": sum(not row["read_positions_correct"] for row in rows),
        "failed_writes": sum(not row["absolute_writes_preserved"] for row in rows),
        "passed": all(row["passed"] for row in rows),
        "observed_kv_length_range": [
            min(row["observed_kv_length"] for row in rows),
            max(row["observed_kv_length"] for row in rows),
        ],
        "failed_examples": [row for row in rows if not row["passed"]][:20],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = summarize(args.directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
