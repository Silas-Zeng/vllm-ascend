"""Plot all frozen primitive metadata controls, including intermediate failures."""

import argparse
import hashlib
import itertools
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def read_results(directory, source):
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    labels = ("Default", "Blocking", "No memory cache", "Both")
    names = ("metadata_gather_stages", "metadata_blocking", "metadata_no_cache", "metadata_both")
    expected = set(itertools.product((193, 224), (17, 32), ("gather", "outofplace", "inplace"), range(30)))
    results = []
    signature = None
    for label, name in zip(labels, names):
        path = directory / (name + ".json")
        report = json.loads(path.read_text(encoding="utf-8"))
        if report["script_sha256"] != digest or report["iterations"] != 30:
            raise ValueError("metadata controls do not match the frozen source or sample count")
        current = (report["scope"], report["torch"], report["torch_npu"], report["device"])
        if signature is not None and current != signature:
            raise ValueError("metadata controls mix runtime or device versions")
        signature = current
        checks = report["checks"]
        cases = [(c["full_width"], c["compact_width"], c["operation"], c["iteration"]) for c in checks]
        if len(cases) != len(expected) or set(cases) != expected:
            raise ValueError("metadata controls contain missing or repeated cases")
        stages = {}
        for check in checks:
            required = {
                "table_copy",
                "length_copy",
                "starts",
                "compact_lengths",
                "columns",
                "indices",
                "mask",
                "selected",
            }
            if check["operation"] == "outofplace":
                required.add("raw_gather")
            if set(check.get("stages", {})) != required:
                raise ValueError("metadata control lacks a completed intermediate check")
            if check["passed"] != all(stage["passed"] for stage in check["stages"].values()):
                raise ValueError("aggregate status disagrees with intermediate results")
            for name, stage in check["stages"].items():
                if stage["passed"] != (stage["mismatches"] == 0):
                    raise ValueError("stage status disagrees with recorded element mismatches")
                counts = stages.setdefault(name, {"failed": 0, "executed": 0})
                counts["executed"] += 1
                counts["failed"] += int(not stage["passed"])
        results.append(
            {
                "label": label,
                "file": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "failed": sum(not c["passed"] for c in checks),
                "executed": len(checks),
                "stages": stages,
            }
        )
    return results


def main(args):
    results = read_results(args.evidence, args.source)
    stages = (
        "table_copy",
        "length_copy",
        "starts",
        "compact_lengths",
        "columns",
        "indices",
        "mask",
        "raw_gather",
        "selected",
    )
    labels = (
        "H2D table",
        "H2D lengths",
        "Window starts",
        "Compact lengths",
        "Columns",
        "Indices",
        "Mask",
        "Raw gather*",
        "Selected table",
    )
    matrix = np.array(
        [
            [result["stages"][stage]["failed"] / result["stages"][stage]["executed"] for result in results]
            for stage in stages
        ]
    )
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    figure, (left, right) = plt.subplots(1, 2, figsize=(11.8, 6.2), gridspec_kw={"width_ratios": [1.45, 1]})
    left.imshow(matrix, cmap="Reds", vmin=0, vmax=0.15, aspect="auto")
    for row, stage in enumerate(stages):
        for column, result in enumerate(results):
            counts = result["stages"][stage]
            left.text(
                column, row, f"{counts['failed']}/{counts['executed']}", ha="center", va="center", color="#263442"
            )
    left.set_yticks(range(len(labels)), labels)
    left.set_xticks(range(4), ["Default", "Blocking", "No cache", "Both"], rotation=15)
    left.set_title("Intermediate mismatch checks / executed checks", fontsize=11, pad=12)
    left.tick_params(length=0, pad=7)
    right.barh(range(4), [x["failed"] for x in results], color="#b74745", height=0.55)
    for index, result in enumerate(results):
        right.text(result["failed"] + 0.8, index, f"{result['failed']}/{result['executed']}", va="center")
    right.set_yticks(range(4), [x["label"] for x in results])
    right.invert_yaxis()
    right.set_xlim(0, 55)
    right.set_xlabel("Trials with any intermediate mismatch")
    right.set_title("Neither control eliminated the issue", fontsize=11, pad=12)
    for axes in (left, right):
        for spine in axes.spines.values():
            spine.set_visible(False)
    figure.suptitle("Integer metadata isolation on Ascend 910B2", x=0.02, ha="left", weight="bold", fontsize=16)
    figure.text(
        0.02,
        0.91,
        "No attention, no model, no timing. Independent Python integer oracle; exact equality required.",
        color="#536477",
    )
    figure.text(
        0.02,
        0.035,
        "Each environment: 193/224 source columns x 17/32 output columns x 3 operator paths x 30 repetitions.\n"
        "*Raw gather is retained separately only in the out-of-place path (120 checks). "
        "Counts are descriptive, not confidence intervals.",
        fontsize=9,
        color="#536477",
    )
    figure.subplots_adjust(left=0.16, right=0.97, top=0.82, bottom=0.20, wspace=0.70)
    args.output.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg"):
        path = args.output / ("metadata_diagnostics." + extension)
        figure.savefig(path, dpi=180, facecolor="white")
        if extension == "svg":
            path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n", encoding="utf-8")
    plt.close(figure)
    (args.output.parent / "metadata_diagnostics_summary.json").write_text(
        json.dumps({"scope": "primitive correctness controls, not model performance", "results": results}, indent=2)
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=Path(__file__).parent / "evidence/server_b2/metadata_stages")
    parser.add_argument("--source", type=Path, default=Path(__file__).parent / "diagnose_npu_gather.py")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "figures")
    main(parser.parse_args())
