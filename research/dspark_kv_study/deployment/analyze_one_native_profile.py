"""Parse one completed TorchNPU SDK capture using its native runtime."""

import argparse
import json
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    from torch_npu.profiler.profiler import analyse

    analyse(str(args.path))
    output = args.path / "ASCEND_PROFILER_OUTPUT"
    names = ("trace_view.json", "operator_details.csv", "kernel_details.csv", "step_trace_time.csv")
    if any(not (output / name).is_file() or not (output / name).stat().st_size for name in names):
        raise ValueError("native profiler did not produce complete analysis tables")
    print(
        json.dumps({"parsed": str(args.path), "output_bytes": {name: (output / name).stat().st_size for name in names}})
    )
