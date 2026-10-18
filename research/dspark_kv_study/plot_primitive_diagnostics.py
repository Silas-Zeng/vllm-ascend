"""Show the observed periodic failures without attributing a physical core."""

import argparse
import hashlib
import itertools
import json
from pathlib import Path

import matplotlib.pyplot as plt


def read_results(directory, source_root):
    operations = (
        "int32_add_tensors",
        "int32_multiply_scalar",
        "int32_multiply_tensors",
        "int64_clamp",
        "int32_gather",
        "int32_mask_multiply",
        "bf16_matmul_integer_inputs",
    )
    expected = set(itertools.product(operations, range(200)))
    digests = {
        name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
        for name in ("diagnose_npu_primitives.py", "diagnose_npu_gather.py")
    }
    results = []
    for name, label in (("native", "Supplied image"), ("official_a2", "Official A2 image via PRoot")):
        path = directory / ("primitives_" + name + ".json")
        report = json.loads(path.read_text(encoding="utf-8"))
        cases = [(c["operation"], c["iteration"]) for c in report["checks"]]
        if report["source_sha256"] != digests or len(cases) != len(expected) or set(cases) != expected:
            raise ValueError("primitive results mix source versions or omit/repeat cases")
        if report["scope"] != "single-operation persistent-input exact correctness; no model or timing":
            raise ValueError("unsupported experiment scope")
        for check in report["checks"]:
            if check["passed"] != (check["mismatches"] == 0):
                raise ValueError("primitive status disagrees with element evidence")
        failures = {
            operation: [c["iteration"] for c in report["checks"] if c["operation"] == operation and not c["passed"]]
            for operation in operations
        }
        results.append(
            {
                "label": label,
                "file": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "failures": failures,
            }
        )
    return operations, results


def main(args):
    operations, results = read_results(args.evidence, args.source_root)
    labels = (
        "int32 add (two tensors)",
        "int32 multiply (scalar)",
        "int32 multiply (two tensors)",
        "int64 clamp",
        "int32 gather",
        "int32 mask multiply",
        "BF16 matrix multiply*",
    )
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    figure, axes = plt.subplots(2, 1, figsize=(11.8, 7.1), sharex=True)
    for axis, result in zip(axes, results):
        for row, operation in enumerate(operations):
            failures = result["failures"][operation]
            axis.axhline(row, color="#e6ebf0", linewidth=1, zorder=0)
            axis.scatter(failures, [row] * len(failures), marker="s", s=24, color="#b74745", zorder=2)
            axis.text(203, row, str(len(failures)) + "/200", va="center", fontsize=9, color="#536477")
        axis.set_yticks(range(len(operations)), labels)
        axis.set_ylim(6.7, -0.7)
        axis.set_xlim(-2, 219)
        axis.set_title(result["label"], loc="left", fontsize=12, weight="bold", pad=12)
        axis.set_xticks([0, 48, 96, 144, 192])
        axis.grid(axis="x", color="#d2dbe4", linestyle="--", linewidth=0.8)
        axis.tick_params(length=0, pad=7)
        for spine in axis.spines.values():
            spine.set_visible(False)
    axes[-1].set_xlabel("Call index (zero based); red squares are exact output mismatches", labelpad=9)
    figure.suptitle(
        "Persistent-input primitive failures repeat every 48 calls", x=0.02, ha="left", fontsize=16, weight="bold"
    )
    figure.text(
        0.02,
        0.92,
        "One operation per call, followed by synchronization. All 2,800 checks are included.",
        color="#536477",
    )
    figure.text(
        0.02,
        0.03,
        "The 48-call interval is observed in tensor add/multiply, clamp and mask multiply. "
        "It is a diagnostic clue, not a proven bad-core ID.\n"
        "*Matrix inputs are small integers exactly representable in BF16; passing this probe "
        "does not establish model or general floating-point correctness.",
        fontsize=9,
        color="#536477",
    )
    figure.subplots_adjust(left=0.26, right=0.97, top=0.84, bottom=0.15, hspace=0.35)
    args.output.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg"):
        path = args.output / ("primitive_periodicity." + extension)
        figure.savefig(path, dpi=180, facecolor="white")
        if extension == "svg":
            path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n", encoding="utf-8")
    plt.close(figure)
    (args.output.parent / "primitive_diagnostics_summary.json").write_text(
        json.dumps(
            {"scope": "exact primitive correctness, no physical-core attribution or timing", "results": results},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence", type=Path, default=Path(__file__).parent / "evidence/server_b2/primitive_controls"
    )
    parser.add_argument("--source-root", type=Path, default=Path(__file__).parent)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "figures")
    main(parser.parse_args())
