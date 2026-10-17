"""Rebuild figures and tables from measured component JSON only."""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path


def load_evidence(paths):
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if len(reports) < 2:
        raise ValueError("at least two complete repeated runs are required")
    first = reports[0]
    case_set = {(case["batch"], case["context_tokens"]) for case in first["cases"]}
    if not case_set:
        raise ValueError("no measured cases")
    planned_cases = {
        (batch, context)
        for batch in first["configuration"]["batches"]
        for context in first["configuration"]["contexts"]
    }
    if case_set != planned_cases:
        raise ValueError("measured cases do not cover configured matrix")
    expected_paths = {"full_kv", "window_2176", "anchors_online", "anchors_cached"}
    configuration = {key: value for key, value in first["configuration"].items() if key not in ("profile", "output")}
    for report in reports:
        if report["scope"] != "synthetic_dense_attention_component_only" or report["is_end_to_end_dspark"]:
            raise ValueError("this plotter accepts component measurements only")
        if report["script_sha256"] != first["script_sha256"] or report["policy_sha256"] != first["policy_sha256"]:
            raise ValueError("measurement code changed between runs")
        if report["environment"] != first["environment"] or report["method"] != first["method"]:
            raise ValueError("environment or workload contract changed between runs")
        current_config = {
            key: value for key, value in report["configuration"].items() if key not in ("profile", "output")
        }
        if current_config != configuration:
            raise ValueError("experiment configuration changed between runs")
        if {(case["batch"], case["context_tokens"]) for case in report["cases"]} != case_set:
            raise ValueError("incomplete case matrix")
        if len(report["cases"]) != len(case_set):
            raise ValueError("duplicate measured cases")
        for case in report["cases"]:
            if set(case["paths"]) != expected_paths or set(case["correctness"]) != expected_paths:
                raise ValueError("incomplete comparison paths")
            if not all(result["passed"] for result in case["correctness"].values()):
                raise ValueError("failed correctness cannot be charted as a valid experiment")
            for result in case["paths"].values():
                samples = result["samples_ms"]
                if len(samples) != configuration["repeats"] or len(samples) < 10:
                    raise ValueError("missing repeated measurements")
                if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0 for value in samples):
                    raise ValueError("invalid timing samples")
    return reports


def make_outputs(paths, output):
    """Validate repeated evidence, then render figures and export numeric tables."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Rectangle

    reports = load_evidence(paths)
    output.mkdir(parents=True, exist_ok=True)
    styles = {
        "full_kv": ("Full KV", "#475569"),
        "window_2176": ("Window 2176", "#0f766e"),
        "anchors_online": ("Anchors: online ranking", "#c2410c"),
        "anchors_cached": ("Anchors: cached indices", "#2563eb"),
    }
    plt.rcParams.update(
        {"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False, "axes.spines.right": False}
    )
    grouped = {}
    rows = []
    for run_id, report in enumerate(reports, 1):
        for case in report["cases"]:
            for name, result in case["paths"].items():
                grouped.setdefault((case["batch"], case["context_tokens"], name), []).extend(result["samples_ms"])
                rows.append(
                    {
                        "run": run_id,
                        "batch": case["batch"],
                        "context_tokens": case["context_tokens"],
                        "path": name,
                        "median_ms": result["median_ms"],
                        "p10_ms": result["p10_ms"],
                        "p90_ms": result["p90_ms"],
                        "correctness_passed": case["correctness"][name]["passed"],
                        "max_absolute_error": case["correctness"][name]["max_absolute_error"],
                    }
                )
    batches = sorted({key[0] for key in grouped})
    contexts = sorted({key[1] for key in grouped})
    summary = []
    for (batch, context, name), values in sorted(grouped.items()):
        summary.append(
            {
                "batch": batch,
                "context_tokens": context,
                "path": name,
                "sample_count": len(values),
                "median_ms": float(np.median(values)),
                "p10_ms": float(np.percentile(values, 10)),
                "p90_ms": float(np.percentile(values, 90)),
            }
        )

    def save_figure(fig, name):
        fig.savefig(output / (name + ".png"), dpi=180, bbox_inches="tight", facecolor="white")
        svg = output / (name + ".svg")
        fig.savefig(svg, bbox_inches="tight", facecolor="white")
        svg.write_text(
            "\n".join(line.rstrip() for line in svg.read_text(encoding="utf-8").splitlines()) + "\n", encoding="utf-8"
        )
        plt.close(fig)

    fig, axes = plt.subplots(1, len(batches), figsize=(11.2, 4.6), squeeze=False)
    for ax, batch in zip(axes[0], batches, strict=True):
        for name, (label, color) in styles.items():
            medians, lows, highs = [], [], []
            for context in contexts:
                values = grouped[(batch, context, name)]
                medians.append(np.median(values))
                lows.append(np.percentile(values, 10))
                highs.append(np.percentile(values, 90))
            ax.plot(np.arange(len(contexts)), medians, "o-", color=color, label=label, lw=2)
            ax.fill_between(np.arange(len(contexts)), lows, highs, color=color, alpha=0.12)
        ax.set(title=f"Batch {batch}", xlabel="Cached context tokens", ylabel="Component wall time (ms)")
        ax.set_xticks(np.arange(len(contexts)), [f"{n // 1024}K" for n in contexts])
        ax.set_ylim(bottom=0)
        ax.grid(axis="y", alpha=0.15)
    axes[0, 0].legend(frameon=False, fontsize=8)
    fig.suptitle("NPU KV selection + unfused attention | synthetic BF16 data", fontsize=13)
    fig.text(
        0.5,
        0.01,
        f"{len(reports)} runs, {reports[0]['configuration']['repeats']} paired samples per path/run. "
        "Shading: empirical P10-P90, not confidence intervals.",
        ha="center",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.04, 1, 0.95))
    save_figure(fig, "component_latency")

    fig, ax = plt.subplots(figsize=(8.5, 4.7))
    x = np.arange(len(contexts))
    for offset, name in zip((-0.2, 0.2), ("anchors_online", "anchors_cached"), strict=True):
        values = [grouped[(max(batches), context, name)] for context in contexts]
        medians = np.array([np.median(v) for v in values])
        errors = np.array(
            [
                [median - np.percentile(v, 10) for v, median in zip(values, medians, strict=True)],
                [np.percentile(v, 90) - median for v, median in zip(values, medians, strict=True)],
            ]
        )
        ax.bar(x + offset, medians, 0.36, yerr=errors, capsize=3, color=styles[name][1], label=styles[name][0])
    ax.set_xticks(x, [f"{n // 1024}K" for n in contexts])
    ax.set(
        xlabel="Cached context tokens",
        ylabel="Component wall time (ms)",
        title=f"Caching request indices: same selected KV | batch {max(batches)}",
    )
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.15)
    fig.text(
        0.5,
        0.01,
        "Cached mode rebuilds local indices and gathers K/V each call. No model throughput is measured.",
        ha="center",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    save_figure(fig, "index_cache_effect")

    fig, ax = plt.subplots(figsize=(11.2, 3.5))
    length = 24576
    selections = [
        ("Full KV", [(0, length)], "#475569"),
        ("Window 2176", [(length - 2176, 2176)], "#0f766e"),
        ("Anchors + local", [(256, 128), (length - 2048, 2048)], "#2563eb"),
    ]
    for row, (label, segments, color) in enumerate(selections):
        ax.add_patch(Rectangle((0, row - 0.23), length, 0.46, color="#e2e8f0"))
        for start, width in segments:
            ax.add_patch(Rectangle((start, row - 0.23), width, 0.46, color=color))
        ax.text(length + 300, row, f"{sum(width for _, width in segments):,} tokens", va="center", fontsize=9)
    ax.set_yticks(range(3), [entry[0] for entry in selections])
    ax.set_xlim(-150, length + 3500)
    ax.set_ylim(-0.6, 2.65)
    ax.invert_yaxis()
    ax.set_xticks([0, 4096, 16384, length], ["0", "4K", "16K", "24K"])
    ax.set(
        xlabel="Absolute cached-token position (draft queries are outside this range)",
        title="Selection schematic: exact token positions, before backend page alignment",
    )
    ax.annotate(
        "128-token historical anchor",
        xy=(320, 2),
        xytext=(3500, 2.45),
        arrowprops={"arrowstyle": "->", "color": "#2563eb"},
        fontsize=9,
    )
    fig.tight_layout()
    save_figure(fig, "selection_policy")

    with (output.parent / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    payload = {
        "scope": "component_only_not_model_throughput",
        "inputs": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        "pooled": summary,
    }
    (output.parent / "summary.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "figures")
    options = parser.parse_args()
    make_outputs(options.inputs, options.output)
