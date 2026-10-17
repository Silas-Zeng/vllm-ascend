"""Verify the isolated package stack and an actual NPU operation."""

import argparse
import hashlib
import importlib
import json
import os
import platform
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

import torch
import torch_npu


def main(output):
    report = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "glibc": platform.libc_ver(),
        "python": platform.python_version(),
        "packages": {},
        "source_files": {},
        "torch_npu_imported_version": torch_npu.__version__,
    }
    for package in ("torch", "torch-npu", "vllm", "vllm-ascend", "transformers", "triton-ascend"):
        report["packages"][package] = metadata.version(package)
    for package in ("vllm", "vllm_ascend"):
        module = importlib.import_module(package)
        report["source_files"][package] = module.__file__
    torch.npu.set_device(0)
    report["npu"] = {"count": torch.npu.device_count(), "device_name": torch.npu.get_device_name(0)}
    tensor = torch.arange(16, dtype=torch.float32).reshape(4, 4)
    actual = (tensor.npu() @ torch.eye(4, device="npu")).cpu()
    torch.testing.assert_close(actual, tensor)
    report["npu_matrix_correctness"] = True
    cann_root = Path(os.environ["ASCEND_TOOLKIT_HOME"])
    report["cann_compiler_version"] = (cann_root / "compiler/version.info").read_text(encoding="utf-8")
    proposer = Path(report["source_files"]["vllm_ascend"]).parent / "spec_decode/llm_base_proposer.py"
    report["llm_base_proposer_sha256"] = hashlib.sha256(proposer.read_bytes()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/results/runtime_probe.json"))
    main(parser.parse_args().output)
