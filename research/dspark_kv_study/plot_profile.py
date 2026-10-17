"""Plot actual CPU annotations and Ascend Hardware kernel events from a trace."""

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

from benchmark_attention import trace_events


def extract_profile(payload):
    events = trace_events(payload)
    hardware_pids = {
        event["pid"]
        for event in events
        if event.get("ph") == "M"
        and event.get("name") == "process_name"
        and event.get("args", {}).get("name") == "Ascend Hardware"
    }
    kernels = [event for event in events if event.get("ph") == "X" and event.get("pid") in hardware_pids]
    ranges = [event for event in events if event.get("ph") == "X" and event.get("name", "").startswith("kv_study/")]
    expected = {"full_kv", "window_2176", "anchors_online", "anchors_cached"}
    if not kernels or {event["name"].split("/", 1)[1] for event in ranges} != expected:
        raise ValueError("trace lacks actual NPU kernels or required measured ranges")
    result = {"scope": "separate_profiled_iteration_not_benchmark_samples", "paths": {}, "time_unit": "microseconds"}
    assigned = set()
    for event in ranges:
        start, duration = float(event["ts"]), float(event["dur"])
        selected = []
        for kernel_id, kernel in enumerate(kernels):
            kernel_start, kernel_duration = float(kernel["ts"]), float(kernel["dur"])
            # Export timestamps have sub-microsecond rounding at epoch scale.
            if kernel_start >= start - 2 and kernel_start + kernel_duration <= start + duration + 2:
                selected.append(
                    {"name": kernel["name"], "start_us": kernel_start - start, "duration_us": kernel_duration}
                )
                assigned.add(kernel_id)
        result["paths"][event["name"].split("/", 1)[1]] = {
            "host_range_us": duration,
            "kernels": selected,
            "sum_kernel_durations_us": sum(kernel["duration_us"] for kernel in selected),
        }
    if len(assigned) != len(kernels):
        raise ValueError("some NPU kernels cannot be assigned to a synchronized user range")
    result["kernel_count"] = len(kernels)
    return result


def kernel_group(name):
    if "IndexSelect" in name:
        return "KV gather"
    if "Slice" in name or "Transpose" in name:
        return "Layout copy"
    if "Matmul" in name:
        return "MatMul"
    if "Softmax" in name:
        return "Softmax"
    if "Cast" in name:
        return "Cast"
    return "Scale / other"


def main(trace, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    data = gzip.decompress(trace.read_bytes()) if trace.suffix == ".gz" else trace.read_bytes()
    report = extract_profile(json.loads(data))
    report["trace_sha256"] = hashlib.sha256(data).hexdigest()
    colors = {
        "KV gather": "#db2777",
        "Layout copy": "#7c3aed",
        "MatMul": "#2563eb",
        "Softmax": "#0f766e",
        "Cast": "#ca8a04",
        "Scale / other": "#64748b",
    }
    titles = {
        "full_kv": "Full KV",
        "window_2176": "Window 2176",
        "anchors_online": "Anchors: online ranking",
        "anchors_cached": "Anchors: cached indices",
    }
    fig, axes = plt.subplots(2, 2, figsize=(11.2, 5.8), sharex=True)
    limit = max(path["host_range_us"] for path in report["paths"].values()) * 1.04
    for ax, (name, path) in zip(axes.flat, report["paths"].items(), strict=True):
        ax.broken_barh([(0, path["host_range_us"])], (0.7, 0.32), facecolors="#cbd5e1")
        counts = Counter()
        for kernel in path["kernels"]:
            group = kernel_group(kernel["name"])
            counts[group] += 1
            ax.broken_barh([(kernel["start_us"], kernel["duration_us"])], (0.12, 0.32), facecolors=colors[group])
        path["kernel_groups"] = dict(counts)
        ax.set(title=titles[name], xlim=(0, limit), ylim=(0, 1.2))
        ax.set_yticks([0.28, 0.86], ["NPU kernels", "Host range"])
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.grid(axis="x", alpha=0.15)
    for ax in axes[1]:
        ax.set_xlabel("Time from path start (microseconds)")
    fig.suptitle("CPU / NPU timeline | batch 4, 24K synthetic KV", fontsize=13)
    fig.legend(
        handles=[Patch(color=color, label=name) for name, color in colors.items()],
        loc="lower center",
        ncol=6,
        frameon=False,
        fontsize=8,
        bbox_to_anchor=(0.5, 0.035),
    )
    fig.text(
        0.5,
        0.01,
        "Separate profiled iteration; profiler overhead is included. Gray is a host scope, not CPU utilization.",
        ha="center",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.10, 1, 0.94))
    output.mkdir(parents=True, exist_ok=True)
    fig.savefig(output / "profile_timeline.png", dpi=180, bbox_inches="tight", facecolor="white")
    svg = output / "profile_timeline.svg"
    fig.savefig(svg, bbox_inches="tight", facecolor="white")
    svg.write_text(
        "\n".join(line.rstrip() for line in svg.read_text(encoding="utf-8").splitlines()) + "\n", encoding="utf-8"
    )
    (output.parent / "profile_summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "figures")
    args = parser.parse_args()
    main(args.trace, args.output)
