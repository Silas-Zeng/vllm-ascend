"""Locate paged-metadata divergence before running attention or a model."""

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path

import torch
import torch_npu


def compare(actual, expected):
    """Return bounded element evidence after device work has completed."""
    observed = actual.cpu()
    different = observed != expected
    locations = different.nonzero().tolist()
    return {
        "passed": not locations,
        "mismatches": len(locations),
        "examples": [
            {"index": index, "expected": expected[tuple(index)].item(), "actual": observed[tuple(index)].item()}
            for index in locations[:8]
        ],
    }


def run_case(full_width, compact_width, operation):
    # Keep host tensors and all device intermediates alive until the final sync.
    lengths = [512, 16391, 24583, 4099]
    starts = [max(length - 2048, 0) // 128 for length in lengths]
    compact_lengths = [length - start * 128 for length, start in zip(lengths, starts)]
    block_counts = [(length + 127) // 128 for length in compact_lengths]
    host_table = torch.tensor(
        [[100 + row * full_width + column for column in range(full_width)] for row in range(len(lengths))],
        dtype=torch.int32,
    )
    host_lengths = torch.tensor(lengths, dtype=torch.int32)
    expected_columns = torch.tensor([list(range(compact_width))], dtype=torch.int64)
    expected_indices = torch.tensor(
        [[min(start + column, full_width - 1) for column in range(compact_width)] for start in starts],
        dtype=torch.int64,
    )
    expected_mask = torch.tensor(
        [[int(column < count) for column in range(compact_width)] for count in block_counts], dtype=torch.int32
    )
    expected_raw = torch.tensor(
        [
            [100 + row * full_width + index for index in indices]
            for row, indices in enumerate(expected_indices.tolist())
        ],
        dtype=torch.int32,
    )
    expected_masked = torch.tensor(
        [
            [value if column < count else 0 for column, value in enumerate(values)]
            for values, count in zip(expected_raw.tolist(), block_counts)
        ],
        dtype=torch.int32,
    )
    device_table = host_table.to("npu", non_blocking=False)
    device_lengths = host_lengths.to("npu", non_blocking=False)
    device_starts = (device_lengths - 2048).clamp(min=0) // 128
    device_compact_lengths = device_lengths - device_starts * 128
    columns = torch.arange(compact_width, device="npu").unsqueeze(0)
    indices = (device_starts.unsqueeze(1) + columns).clamp(max=full_width - 1)
    mask = (columns < ((device_compact_lengths + 127) // 128).unsqueeze(1)).to(torch.int32)
    gathered = device_table.gather(1, indices)
    if operation == "inplace":
        gathered *= mask
        selected = gathered
    elif operation == "outofplace":
        selected = gathered * mask
    else:
        selected = gathered
    torch.npu.synchronize()
    tensors = {
        "table_copy": (device_table, host_table),
        "length_copy": (device_lengths, host_lengths),
        "starts": (device_starts, torch.tensor(starts, dtype=torch.int32)),
        "compact_lengths": (device_compact_lengths, torch.tensor(compact_lengths, dtype=torch.int32)),
        "columns": (columns, expected_columns),
        "indices": (indices, expected_indices),
        "mask": (mask, expected_mask),
        "selected": (selected, expected_raw if operation == "gather" else expected_masked),
    }
    if operation == "outofplace":
        tensors["raw_gather"] = (gathered, expected_raw)
    checks = {name: compare(actual, expected) for name, (actual, expected) in tensors.items()}
    return {"passed": all(check["passed"] for check in checks.values()), "stages": checks}


def main(args):
    torch.npu.set_device(0)
    report = {
        "scope": "integer metadata correctness isolation; no attention, model, or performance measurement",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "torch": torch.__version__,
        "torch_npu": torch_npu.__version__,
        "device": torch.npu.get_device_name(0),
        "launch_blocking": os.environ.get("ASCEND_LAUNCH_BLOCKING"),
        "iterations": args.iterations,
        "checks": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for full_width, compact_width, operation in itertools.product(
        (193, 224), (17, 32), ("gather", "outofplace", "inplace")
    ):
        for iteration in range(args.iterations):
            record = {
                "full_width": full_width,
                "compact_width": compact_width,
                "operation": operation,
                "iteration": iteration,
            }
            try:
                record.update(run_case(full_width, compact_width, operation))
            except Exception as error:
                record.update(passed=False, error=repr(error))
            report["checks"].append(record)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        checks = report["checks"][-args.iterations :]
        print(
            json.dumps(
                {
                    "full_width": full_width,
                    "compact_width": compact_width,
                    "operation": operation,
                    "passed": sum(x["passed"] for x in checks),
                    "total": len(checks),
                }
            ),
            flush=True,
        )
    if not all(check["passed"] for check in report["checks"]):
        raise AssertionError("Metadata differs from independent integer oracle; inspect recorded stage evidence")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
