"""Plot verified context-stage summaries and original CPU/NPU trace events."""

import argparse
import gzip
import json
from decimal import Decimal
from pathlib import Path

COLORS = {"ar": "#566573", "full": "#1677b8", "window": "#de8130"}
LABELS = {"ar": "AR", "full": "DSpark Full KV", "window": "DSpark Window 2176 (fix)"}


def scope(item, name, field):
    return item["scopes"].get("study/" + name, {}).get(field)


def per_draft_call(item, name):
    value = scope(item, name + "/decode", "device_total_us")
    calls = scope(item, "draft_total/decode", "calls")
    if value is None or not calls:
        raise ValueError("missing pure draft stage " + name)
    return value / calls / 1000


def draft_parts(item):
    attention = per_draft_call(item, "draft_attention")
    backbone = per_draft_call(item, "draft_backbone")
    total = per_draft_call(item, "draft_total")
    parts = {
        "Inputs + metadata": per_draft_call(item, "draft_inputs") + per_draft_call(item, "draft_metadata"),
        "Context KV construction": per_draft_call(item, "draft_context_cache"),
        "Attention path": attention,
        "Backbone outside attention": backbone - attention,
        "Draft LM head": per_draft_call(item, "draft_logits"),
        "Markov embedding + bias": per_draft_call(item, "markov_embed") + per_draft_call(item, "markov_bias"),
    }
    parts["Other draft tasks"] = total - sum(parts.values())
    if any(value < -0.005 for value in parts.values()):
        raise ValueError("overlapping/missing draft association for " + item["label"])
    return {name: max(0, value) for name, value in parts.items()}


def save(fig, output, name):
    for extension in ("png", "svg"):
        fig.savefig(output / (name + "." + extension), dpi=180)


def plot_stages(items, output):
    """Keep host/device measures separate and subtract nested attention exactly once."""
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 2, figsize=(12, 11), constrained_layout=True)
    for column, batch in enumerate((1, 4)):
        for method in ("ar", "full", "window"):
            rows = [item for item in items if item["concurrency"] == batch and item["method"] == method]
            axes[0, column].plot(
                [4, 16, 24],
                [r["target_attribution"]["pure_forward_device_ms_per_call"] for r in rows],
                marker="o",
                color=COLORS[method],
                label=LABELS[method],
            )
            phase = "decode" if method == "ar" else "verify"
            axes[1, column].plot(
                [4, 16, 24],
                [r["target_attribution"]["graph_fia_ms_per_replay"] for r in rows],
                marker="o",
                color=COLORS[method],
            )
            axes[2, column].plot(
                [4, 16, 24],
                [scope(r, "target_" + phase, "host_total_ms_per_call") for r in rows],
                marker="o",
                color=COLORS[method],
            )
        axes[0, column].set(
            title=f"Concurrency {batch}: Target NPU tasks", ylabel="cumulative ms / pure target invocation"
        )
        axes[1, column].set(
            title="Target graph fused attention (36 layers)", ylabel="cumulative NPU ms / actual graph replay"
        )
        axes[2, column].set(
            title="Target host scope (dispatch; may include waiting)", ylabel="host ms / pure target invocation"
        )
    for axis in axes.flat:
        axis.set(xlabel="Input length (Ki tokens)", xticks=[4, 16, 24], ylim=(0, None))
        axis.grid(alpha=0.2)
    axes[0, 0].legend(fontsize=8)
    fig.suptitle(
        "Target forward on the small-query attention path (including startup/tail steps)\n"
        "Graph kernel totals recovered by model ID and validated against 36 FIA calls per replay",
        fontsize=11,
    )
    save(fig, output, "phase_target")
    plt.close(fig)

    fig, axes = plt.subplots(2, 3, figsize=(15, 8), constrained_layout=True)
    columns = [
        ("Draft NPU tasks", lambda r: per_draft_call(r, "draft_total"), "cumulative NPU ms / batch draft invocation"),
        (
            "Draft attention path (5 layers)",
            lambda r: per_draft_call(r, "draft_attention"),
            "cumulative NPU ms / batch draft invocation",
        ),
        (
            "Draft metadata host scope",
            lambda r: scope(r, "draft_metadata/decode", "host_total_ms_per_call"),
            "host ms / batch draft invocation",
        ),
    ]
    for row_index, batch in enumerate((1, 4)):
        for column, (title, getter, units) in enumerate(columns):
            axis = axes[row_index, column]
            for method in ("full", "window"):
                rows = [r for r in items if r["concurrency"] == batch and r["method"] == method]
                axis.plot(
                    [4, 16, 24], [getter(r) for r in rows], color=COLORS[method], marker="o", label=LABELS[method]
                )
            axis.set(
                title=f"C{batch}: {title}",
                xlabel="Input length (Ki tokens)",
                ylabel=units,
                xticks=[4, 16, 24],
                ylim=(0, None),
            )
            axis.grid(alpha=0.2)
    axes[0, 0].legend(fontsize=8)
    fig.suptitle(
        "Draft costs on the small-query attention path\n"
        "Separate profiles; device task totals and host scopes are different measures; host scopes include waits",
        fontsize=11,
    )
    save(fig, output, "phase_draft")
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.4), constrained_layout=True)
    colors = ["#9da9b1", "#9271bb", "#1677b8", "#69b6c3", "#d59d23", "#de8130", "#a5ba74"]
    for axis, batch in zip(axes, (1, 4)):
        rows = [
            next(r for r in items if r["concurrency"] == batch and r["context"] == context and r["method"] == method)
            for context in (4096, 16384, 24576)
            for method in ("full", "window")
        ]
        values = [draft_parts(r) for r in rows]
        bottom = [0.0] * len(rows)
        for (part, _), color in zip(values[0].items(), colors):
            heights = [v[part] for v in values]
            axis.bar(range(6), heights, bottom=bottom, color=color, label=part)
            bottom = [a + b for a, b in zip(bottom, heights)]
        axis.set(
            title=f"Concurrency {batch}",
            ylabel="cumulative NPU ms / batch draft invocation",
            xticks=range(6),
            xticklabels=["4K\nFull", "4K\nWin", "16K\nFull", "16K\nWin", "24K\nFull", "24K\nWin"],
        )
        axis.grid(axis="y", alpha=0.2)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=4, fontsize=8)
    fig.suptitle(
        "Draft NPU task decomposition\n"
        "Attention is subtracted from backbone; parent and child scopes are not counted twice",
        fontsize=11,
    )
    save(fig, output, "phase_draft_parts")
    plt.close(fig)


def timeline_events(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        data = json.load(stream, parse_float=Decimal)
    return [event for event in data["traceEvents"] if event.get("ph") == "X"]


def relative_interval(timestamp, duration, origin, end):
    start = Decimal(str(timestamp))
    width = Decimal(str(duration))
    if width <= 0 or start + width < origin or start > end:
        return None
    left = max(start, origin)
    right = min(start + width, end)
    return float((left - origin) / 1000), float((right - left) / 1000)


def hardware_row(event, graph_models):
    """Keep device waits/control and DMA events separate from actual kernels."""
    args = event["args"]
    kind = args["Task Type"]
    if kind in ("PCIE_DMA_SQE", "SDMA_SQE"):
        return 7
    if kind not in ("AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU"):
        return 8
    model = str(args.get("Model Id"))
    if model in graph_models:
        return 4
    if model == "4294967295" and "InferAttention" in event["name"]:
        return 5
    return 6


def plot_timeline(profiles, output, labels, name, attributions):
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(len(labels), 1, figsize=(14, 3.8 * len(labels)), constrained_layout=True)
    if len(labels) == 1:
        axes = [axes]
    for axis, label in zip(axes, labels):
        events = timeline_events(profiles / label / "compact.trace.json.gz")
        pure = sorted(
            [e for e in events if e["name"] in ("study/target_verify", "study/target_decode")],
            key=lambda e: Decimal(str(e["ts"])),
        )
        if len(pure) < 12:
            raise ValueError("insufficient pure target iterations for " + label)
        origin = Decimal(str(pure[10]["ts"])) - 5000
        end = origin + 120000
        rows = [[] for _ in range(9)]
        for event in events:
            interval = relative_interval(event["ts"], event.get("dur", 0), origin, end)
            if interval is None:
                continue
            event_name = event["name"]
            args = event.get("args", {})
            row = None
            if event_name in ("study/target_verify", "study/target_decode"):
                row = 0
            elif event_name == "study/draft_metadata/decode":
                row = 1
            elif event_name == "study/draft_backbone/decode":
                row = 2
            elif event_name == "study/draft_generate/decode":
                row = 3
            elif "Task Type" in args:
                row = hardware_row(event, attributions[label])
            if row is not None:
                rows[row].append(interval)
        for row, segments in enumerate(rows):
            axis.broken_barh(
                segments,
                (row - 0.3, 0.6),
                facecolors=[
                    "#566573",
                    "#9271bb",
                    "#69b6c3",
                    "#a5ba74",
                    "#1677b8",
                    "#de8130",
                    "#d59d23",
                    "#ba82b1",
                    "#adb5bd",
                ][row],
            )
        axis.set(
            title=label + " (120 ms window, starting 5 ms before the 11th small-query target call)",
            xlabel="ms from selected trace origin",
            xlim=(0, 120),
            yticks=range(9),
            yticklabels=[
                "Target host",
                "Metadata host",
                "Backbone host",
                "Generate host (nested)",
                "Target graph kernels",
                "Eager FIA kernels",
                "Other device kernels",
                "DMA copy tasks",
                "Other / wait / control",
            ],
        )
        axis.invert_yaxis()
        axis.grid(axis="x", alpha=0.2)
    fig.suptitle(
        "Original CPU/NPU events on a shared clock\n"
        "Host rows overlap and include waits; device kernels, DMA and wait/control tasks are shown separately",
        fontsize=11,
    )
    save(fig, output, name)
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import matplotlib

    matplotlib.use("Agg")
    args.output.mkdir(parents=True, exist_ok=True)
    items = json.loads(args.summary.read_text())["configurations"]
    if len(items) != 18:
        raise ValueError("profile matrix is incomplete")
    plot_stages(items, args.output)
    attributions = {
        item["label"]: set(item["target_attribution"]["graph_models_with_target_attention"]) for item in items
    }
    plot_timeline(args.profiles, args.output, ["full_c24576_b1", "window_c24576_b1"], "timeline_24k_c1", attributions)
    plot_timeline(args.profiles, args.output, ["full_c24576_b4", "window_c24576_b4"], "timeline_24k_c4", attributions)
