"""Plot all independent FIA controls, including failures and unmeasured cells."""

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap


def read_controls(directory, source_root=None):
    source_root = source_root or Path(__file__).parent / "archive/fia_shape_controls"
    records, signature = [], None
    for batch in (3, 4):
        for queries in (7, 8):
            for policy in ("full", "window") if batch == 3 else ("window", "bm25"):
                path = directory / f"{policy}_b{batch}_q{queries}_fia.json"
                data = json.loads(path.read_text(encoding="utf-8"))
                if (
                    data.get("scope")
                    != "synthetic real paged FIA numerical validation; not full-model or latency evidence"
                ):
                    raise ValueError("the plot accepts only real paged FIA numerical controls")
                current_signature = (data["device"], data["torch_npu"], data["source_sha256"])
                names = {"validate_paged_history_npu.py", "paged_history.py", "history_policy.py"}
                if set(data["source_sha256"]) != names or any(
                    hashlib.sha256((source_root / name).read_bytes()).hexdigest() != data["source_sha256"][name]
                    for name in names
                ):
                    raise ValueError("controls do not match the supplied source snapshot")
                if signature is None:
                    signature = current_signature
                if signature != current_signature:
                    raise ValueError("controls have different devices, runtimes or source versions")
                if len(data.get("checks", [])) != 1:
                    raise ValueError("each control must contain exactly one executed check")
                check = data["checks"][0]
                lengths = check.get("input_lengths_including_queries", check.get("lengths"))
                if len(lengths) != batch or check["policy"] != policy or check["query_tokens"] != queries:
                    raise ValueError("the recorded case differs from the expected control")
                if not isinstance(check.get("passed"), bool):
                    raise ValueError("control lacks an explicit validation outcome")
                if check["passed"] and not (
                    check.get("full_cache_unchanged")
                    and check.get("absolute_slots_and_positions_unchanged")
                    and np.isfinite(check["max_abs_error"])
                ):
                    raise ValueError("successful control lacks cache and numerical checks")
                if not check["passed"] and not check.get("error"):
                    raise ValueError("failed control lacks its error evidence")
                records.append(
                    {
                        "batch": batch,
                        "queries": queries,
                        "policy": policy,
                        "passed": check["passed"],
                        "error": check.get("error"),
                        "max_abs_error": check.get("max_abs_error"),
                        "file": path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
    return records


def main(args):
    records = read_controls(args.evidence, args.source_root)
    matrix = np.zeros((4, 3), dtype=int)
    policies = {"full": 0, "window": 1, "bm25": 2}
    for record in records:
        row = (record["batch"] - 3) * 2 + record["queries"] - 7
        matrix[row, policies[record["policy"]]] = 2 if record["passed"] else 1
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11})
    figure, axes = plt.subplots(figsize=(8.5, 4.8))
    axes.imshow(matrix, cmap=ListedColormap(["#edf0f4", "#f8d5d1", "#bce7d2"]), vmin=0, vmax=2, aspect="auto")
    for row in range(4):
        for column in range(3):
            label = ("Not run", "FAIL", "PASS")[matrix[row, column]]
            axes.text(column, row, label, ha="center", va="center", weight="bold", color="#243447")
    axes.set_xticks(range(3), ["Full KV", "Local window", "History + window"])
    axes.set_yticks(range(4), ["B3 / 7 queries", "B3 / 8 queries", "B4 / 7 queries", "B4 / 8 queries"])
    axes.set_xticks(np.arange(-0.5, 3, 1), minor=True)
    axes.set_yticks(np.arange(-0.5, 4, 1), minor=True)
    axes.grid(which="minor", color="white", linewidth=3)
    axes.tick_params(which="both", length=0, pad=10)
    for spine in axes.spines.values():
        spine.set_visible(False)
    figure.suptitle("Paged FIA correctness controls on Ascend 910B2", x=0.03, ha="left", fontsize=15, weight="bold")
    figure.text(0.03, 0.89, "One fresh process per cell. Passing once is not evidence of stability.", color="#526477")
    figure.text(
        0.03,
        0.045,
        "B3 lengths: 512 / 16391 / 24583; B4 adds 4099. Lengths include queries.\n"
        "FAIL includes wrong numerical output or a device exception. This figure contains no timing results.",
        fontsize=9,
        color="#526477",
    )
    figure.subplots_adjust(left=0.24, right=0.97, top=0.81, bottom=0.21)
    args.output.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg"):
        path = args.output / f"backend_validation.{extension}"
        figure.savefig(path, dpi=180, facecolor="white")
        if extension == "svg":
            path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n", encoding="utf-8")
    plt.close(figure)
    summary = {"scope": "individual numerical controls; not throughput or stability", "records": records}
    (args.output.parent / "backend_validation_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).parent / "archive/fia_shape_controls")
    main(parser.parse_args())
