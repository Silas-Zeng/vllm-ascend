"""Check one operation at a time with persistent inputs and per-call sync."""

import argparse
import hashlib
import json
from pathlib import Path

import torch
import torch_npu
from diagnose_npu_gather import compare


def cases():
    host = (torch.arange(68, dtype=torch.int32).reshape(4, 17) % 97) - 48
    host_other = torch.full_like(host, 128)
    device, other = host.npu(), host_other.npu()
    wide_host = torch.arange(4 * 193, dtype=torch.int32).reshape(4, 193) + 100
    index_host = torch.tensor([[start + column for column in range(17)] for start in [0, 112, 176, 16]])
    wide, index = wide_host.npu(), index_host.npu()
    int64 = host.to(torch.int64).npu()
    mask_host = torch.tensor(
        [[int(column < count) for column in range(17)] for count in [4, 17, 17, 17]], dtype=torch.int32
    )
    mask = mask_host.npu()
    generator = torch.Generator().manual_seed(9382)
    a_host = (torch.randint(0, 2, (16, 16), generator=generator) * 2 - 1).bfloat16()
    b_host = (torch.randint(0, 2, (16, 16), generator=generator) * 2 - 1).bfloat16()
    a, b = a_host.npu(), b_host.npu()
    # Every input remains referenced by its closure throughout all repetitions.
    return {
        "int32_add_tensors": (lambda: torch.add(device, other), host + host_other),
        "int32_multiply_scalar": (lambda: torch.mul(device, 128), host * 128),
        "int32_multiply_tensors": (lambda: torch.mul(device, other), host * host_other),
        "int64_clamp": (lambda: torch.clamp(int64, min=0, max=31), host.to(torch.int64).clamp(min=0, max=31)),
        "int32_gather": (lambda: torch.gather(wide, 1, index), wide_host.gather(1, index_host)),
        "int32_mask_multiply": (lambda: torch.mul(device, mask), host * mask_host),
        "bf16_matmul_integer_inputs": (lambda: torch.matmul(a, b), (a_host.float() @ b_host.float()).bfloat16()),
    }


def main(args):
    torch.npu.set_device(0)
    report = {
        "scope": "single-operation persistent-input exact correctness; no model or timing",
        "source_sha256": {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in (Path(__file__).name, "diagnose_npu_gather.py")
        },
        "torch": torch.__version__,
        "torch_npu": torch_npu.__version__,
        "device": torch.npu.get_device_name(0),
        "checks": [],
    }
    operations = cases()
    torch.npu.synchronize()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for name, (operation, expected) in operations.items():
        for iteration in range(args.iterations):
            actual = operation()
            torch.npu.synchronize()
            record = compare(actual, expected)
            report["checks"].append({"operation": name, "iteration": iteration, **record})
        checks = report["checks"][-args.iterations :]
        print(
            json.dumps({"operation": name, "passed": sum(c["passed"] for c in checks), "total": len(checks)}),
            flush=True,
        )
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not all(check["passed"] for check in report["checks"]):
        raise AssertionError("Persistent-input primitive operation differs from exact CPU reference")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
