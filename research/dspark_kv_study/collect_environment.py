"""Collect a bounded, credential-free environment record for this experiment."""

import argparse
import json
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path


def main(output, cann_root):
    report = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "glibc": platform.libc_ver(),
        "python": platform.python_version(),
        "packages": {},
        "containers": {name: shutil.which(name) is not None for name in ("docker", "podman")},
        "cann_version_files": {},
    }
    for package in ("torch", "torch-npu", "vllm", "vllm-ascend", "transformers"):
        try:
            report["packages"][package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            report["packages"][package] = None
    for relative in ("compiler/version.info", "opp/version.info"):
        path = cann_root / relative
        if path.is_file():
            report["cann_version_files"][relative] = path.read_text(encoding="utf-8")[:8192]
    driver = Path("/usr/local/Ascend/driver/version.info")
    if driver.is_file():
        report["driver_version_info"] = driver.read_text(encoding="utf-8")[:8192]
    proc = subprocess.run(["npu-smi", "info"], capture_output=True, text=True, timeout=20, check=False)
    report["npu_smi"] = {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    report["local_model_cache_roots_checked"] = {}
    for root in (Path.home() / ".cache/huggingface/hub", Path("/models"), Path("/data/models")):
        report["local_model_cache_roots_checked"][str(root)] = (
            [path.name for path in root.iterdir() if "qwen" in path.name.lower() or "dspark" in path.name.lower()]
            if root.is_dir()
            else []
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cann-root", type=Path, required=True)
    args = parser.parse_args()
    main(args.output, args.cann_root)
