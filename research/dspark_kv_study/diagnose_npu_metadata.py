"""Isolate H2D metadata transfer and page-index arithmetic without attention."""

import argparse
import hashlib
import json
from pathlib import Path

import torch
import torch_npu


def main(output):
    torch.npu.set_device(0)
    report = {
        "scope": "metadata transfer/arithmetic diagnostics, no attention or timing",
        "torch_npu": torch_npu.__version__,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checks": [],
    }
    lengths = [512, 16391, 24583, 4099]
    host_lengths = torch.tensor(lengths, dtype=torch.int32)
    host_table = torch.arange(4 * 193, dtype=torch.int32).reshape(4, 193) + 100
    expected_starts = (host_lengths - 2048).clamp(min=0) // 128
    expected_lengths = host_lengths - expected_starts * 128
    columns_cpu = torch.arange(17).unsqueeze(0)
    expected_table = host_table.gather(1, expected_starts.unsqueeze(1) + columns_cpu)
    expected_table *= (columns_cpu < ((expected_lengths + 127) // 128).unsqueeze(1)).to(torch.int32)
    output.parent.mkdir(parents=True, exist_ok=True)
    for transfer in ("constructor", "persistent_host"):
        for iteration in range(20):
            device_lengths = (
                torch.tensor(lengths, dtype=torch.int32, device="npu")
                if transfer == "constructor"
                else host_lengths.to("npu", non_blocking=False)
            )
            device_table = host_table.to("npu", non_blocking=False)
            initial = device_lengths.cpu()
            starts = (device_lengths - 2048).clamp(min=0) // 128
            compact = device_lengths - starts * 128
            columns = torch.arange(17, device="npu").unsqueeze(0)
            indices = starts.unsqueeze(1) + columns
            selected = device_table.gather(1, indices)
            selected *= (columns < ((compact + 127) // 128).unsqueeze(1)).to(torch.int32)
            torch.npu.synchronize()
            observed = {"input": initial.tolist(), "starts": starts.cpu().tolist(), "lengths": compact.cpu().tolist()}
            passed = (
                torch.equal(initial, host_lengths)
                and torch.equal(starts.cpu(), expected_starts)
                and torch.equal(compact.cpu(), expected_lengths)
                and torch.equal(selected.cpu(), expected_table)
            )
            report["checks"].append({"transfer": transfer, "iteration": iteration, "passed": bool(passed), **observed})
            output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"checks": len(report["checks"]), "passed": sum(check["passed"] for check in report["checks"])}))
    if not all(check["passed"] for check in report["checks"]):
        raise AssertionError("NPU metadata transfer or arithmetic differs from its CPU oracle")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args().output)
