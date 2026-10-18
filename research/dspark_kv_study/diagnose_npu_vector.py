"""Check exactly representable floating-point vector operations and their inputs."""

import argparse
import hashlib
import json
from pathlib import Path

import torch
import torch_npu
from diagnose_npu_gather import compare


def cases(dtype, width):
    host = ((torch.arange(4 * width).reshape(4, width) % 64) - 32).to(dtype)
    other_host = torch.full_like(host, 128)
    table_host = (torch.arange(4 * 193).reshape(4, 193) + 100).to(dtype)
    indices_host = torch.tensor([[min(start + column, 192) for column in range(width)] for start in [0, 112, 176, 16]])
    device, other, table, indices = host.npu(), other_host.npu(), table_host.npu(), indices_host.npu()
    return {
        "add": (lambda: device + other, host + other_host, ((device, host), (other, other_host))),
        "multiply": (lambda: device * other, host * other_host, ((device, host), (other, other_host))),
        "clamp": (lambda: device.clamp(min=0, max=31), host.clamp(min=0, max=31), ((device, host),)),
        "gather": (
            lambda: table.gather(1, indices),
            table_host.gather(1, indices_host),
            ((table, table_host), (indices, indices_host)),
        ),
    }


def main(args):
    torch.npu.set_device(0)
    props = torch.npu.get_device_properties(0)
    report = {
        "scope": "exactly representable FP32/BF16 vector correctness, no model or timing",
        "source_sha256": {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in (Path(__file__).name, "diagnose_npu_gather.py")
        },
        "torch_npu": torch_npu.__version__,
        "device": props.name,
        "cube_core_num": props.cube_core_num,
        "vector_core_num": props.vector_core_num,
        "checks": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for dtype in (torch.float32, torch.bfloat16):
        for width in (17, 32):
            operations = cases(dtype, width)
            torch.npu.synchronize()
            for name, (operation, expected, inputs) in operations.items():
                for iteration in range(args.iterations):
                    actual = operation()
                    torch.npu.synchronize()
                    record = compare(actual, expected)
                    if not record["passed"]:
                        record["input_checks_after_failure"] = [compare(device, host) for device, host in inputs]
                        record["output_aliases_input"] = any(
                            actual.data_ptr() == device.data_ptr() for device, _ in inputs
                        )
                    report["checks"].append(
                        {"dtype": str(dtype), "width": width, "operation": name, "iteration": iteration, **record}
                    )
                checks = report["checks"][-args.iterations :]
                print(
                    json.dumps(
                        {
                            "dtype": str(dtype),
                            "width": width,
                            "operation": name,
                            "failed": sum(not c["passed"] for c in checks),
                            "total": len(checks),
                        }
                    ),
                    flush=True,
                )
                args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not all(check["passed"] for check in report["checks"]):
        raise AssertionError("Exactly representable vector results differ; input checks recorded for failed calls")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
