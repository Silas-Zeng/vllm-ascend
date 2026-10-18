"""Summarize native profiler tables without summing nested scopes as elapsed time."""

import argparse
import csv
import gzip
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path


def numeric(value):
    if value is None or str(value).strip() in ("", "N/A", "NA", "nan", "None"):
        return None
    number = float(str(value).strip())
    return number if math.isfinite(number) else None


def read_csv(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def summarize_scopes(rows):
    result = {}
    columns = {
        "host_total_us": "Host Total Duration(us)",
        "host_self_us": "Host Self Duration(us)",
        "device_total_us": "Device Total Duration(us)",
        "device_aicore_total_us": "Device Total Duration With AICore(us)",
    }
    for row in rows:
        name = row["Name"]
        if not name.startswith("study/"):
            continue
        item = result.setdefault(name, {"calls": 0, **{key: 0.0 for key in columns}})
        item["calls"] += 1
        for key, column in columns.items():
            value = numeric(row.get(column))
            if value is None:
                item[key] = None
            elif item[key] is not None:
                item[key] += value
    for item in result.values():
        for key in columns:
            item[key.replace("_us", "_ms_per_call")] = (
                item[key] / item["calls"] / 1000 if item[key] is not None else None
            )
    return result


def summarize_kernels(rows):
    types = defaultdict(lambda: {"calls": 0, "duration_us": 0.0})
    models = defaultdict(lambda: {"calls": 0, "duration_us": 0.0})
    fia_models = defaultdict(lambda: {"calls": 0, "duration_us": 0.0, "shapes": {}})
    utilization = defaultdict(list)
    for row in rows:
        duration = numeric(row.get("Duration(us)"))
        if duration is None or duration < 0:
            raise ValueError("missing/negative kernel duration")
        kind = row["Type"].strip()
        model = row["Model ID"].strip()
        for item in (types[kind], models[model]):
            item["calls"] += 1
            item["duration_us"] += duration
        if "InferAttention" in kind:
            item = fia_models[model]
            item["calls"] += 1
            item["duration_us"] += duration
            shape = row.get("Input Shapes", "").strip()
            item["shapes"][shape] = item["shapes"].get(shape, 0) + 1
        for key in ("cube_utilization(%)", "aic_mac_ratio", "aiv_vec_ratio"):
            value = numeric(row.get(key))
            if value is not None:
                utilization[key].append((value, duration))
    total = sum(item["duration_us"] for item in types.values())
    for item in types.values():
        item["cumulative_duration_share"] = item["duration_us"] / total if total else None
    return {
        "cumulative_task_time_us": total,
        "types": dict(sorted(types.items(), key=lambda pair: -pair[1]["duration_us"])),
        "model_ids": dict(models),
        "fia_by_model_id": dict(fia_models),
        "available_duration_weighted_utilization": {
            key: {"sample_count": len(values), "mean": sum(v * d for v, d in values) / sum(d for _, d in values)}
            for key, values in utilization.items()
            if sum(d for _, d in values) > 0
        },
    }


def target_graph_attribution(scopes, kernels, phases):
    executions = {}
    for phase, calls in phases.items():
        executions[phase] = {
            mode: scopes.get(f"study/target_{mode}/{phase}", {}).get("calls", 0)
            for mode in ("graph", "eager", "capture", "unknown")
        }
        if sum(executions[phase].values()) != calls:
            raise ValueError("target execution mode does not partition phase " + phase)
        if executions[phase]["capture"] or executions[phase]["unknown"]:
            raise ValueError("profile contains uncategorized mode or an unwarmed target graph")
    if executions["prefill"]["graph"] or executions["mixed"]["graph"]:
        raise ValueError("FULL_DECODE_ONLY unexpectedly used a graph for prefill/mixed")
    graph_calls = sum(item["graph"] for item in executions.values())
    graph_models = {model: item for model, item in kernels["fia_by_model_id"].items() if model != "4294967295"}
    graph_fia_calls = sum(item["calls"] for item in graph_models.values())
    if graph_fia_calls != 36 * graph_calls:
        raise ValueError("graph attention count does not match 36 layers times actual replay calls")
    graph_us = sum(kernels["model_ids"][model]["duration_us"] for model in graph_models)
    eager_pure_us = sum(
        scopes.get(f"study/target_eager/{phase}", {}).get("device_total_us", 0) for phase in ("decode", "verify")
    )
    pure_calls = phases["decode"] + phases["verify"]
    return {
        "execution_counts": executions,
        "graph_models_with_target_attention": list(graph_models),
        "graph_fia_calls": graph_fia_calls,
        "graph_device_cumulative_us": graph_us,
        "eager_pure_device_cumulative_us": eager_pure_us,
        "pure_target_calls": pure_calls,
        "pure_forward_device_ms_per_call": (graph_us + eager_pure_us) / pure_calls / 1000 if pure_calls else None,
        "graph_device_ms_per_replay": graph_us / graph_calls / 1000 if graph_calls else None,
        "graph_fia_ms_per_replay": (
            sum(item["duration_us"] for item in graph_models.values()) / graph_calls / 1000 if graph_calls else None
        ),
        "scope_device_graph_association_complete": False,
    }


def summarize(label, folder):
    operators = folder / "operator_details.csv"
    kernels = folder / "kernel_details.csv"
    scopes = summarize_scopes(read_csv(operators))
    if not scopes.get("study/target_forward"):
        raise ValueError("missing target annotations: " + label)
    if scopes.get("study/target_unknown", {}).get("calls", 0):
        raise ValueError("unclassified target iterations: " + label)
    phases = {
        name: scopes.get("study/target_" + name, {}).get("calls", 0)
        for name in ("prefill", "mixed", "decode", "verify")
    }
    if sum(phases.values()) != scopes["study/target_forward"]["calls"]:
        raise ValueError("phase counts do not partition target calls: " + label)
    method, context, batch = label.split("_")
    if method != "ar" and not scopes.get("study/draft_total/decode"):
        raise ValueError("no pure decode draft iterations: " + label)
    kernel_summary = summarize_kernels(read_csv(kernels))
    attribution = target_graph_attribution(scopes, kernel_summary, phases)
    return {
        "label": label,
        "method": method,
        "context": int(context[1:]),
        "concurrency": int(batch[1:]),
        "phase_counts": phases,
        "scopes": scopes,
        "kernels": kernel_summary,
        "target_attribution": attribution,
        "step_trace": list(read_csv(folder / "step_trace_time.csv")),
        "source_sha256": {
            name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
            for name in ("operator_details.csv", "kernel_details.csv", "step_trace_time.csv")
        },
    }


def export_scope_csv(items, path):
    fields = [
        "label",
        "scope",
        "calls",
        "host_total_us",
        "host_self_us",
        "device_total_us",
        "device_aicore_total_us",
        "host_total_ms_per_call",
        "host_self_ms_per_call",
        "device_total_ms_per_call",
        "device_aicore_total_ms_per_call",
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in items:
            for name, values in item["scopes"].items():
                writer.writerow({"label": item["label"], "scope": name, **values})


def main(args):
    items = []
    for batch in (1, 4):
        for context in (4096, 16384, 24576):
            for method in ("ar", "full", "window"):
                label = f"{method}_c{context}_b{batch}"
                candidates = list((args.traces / label).glob("*_ascend_pt/ASCEND_PROFILER_OUTPUT"))
                if len(candidates) != 1:
                    raise ValueError(f"expected one finalized profile for {label}, got {len(candidates)}")
                item = summarize(label, candidates[0])
                items.append(item)
                print(
                    json.dumps(
                        {
                            "label": label,
                            "phases": item["phase_counts"],
                            "graph_fia_count_valid": True,
                        }
                    ),
                    flush=True,
                )
    args.output.mkdir(parents=True, exist_ok=True)
    result = {
        "scope": "first 64 worker iterations of one separate profile run; includes prefill and a partial decode",
        "interpretation": (
            "Device totals are cumulative associated task durations, not elapsed time. "
            "Host durations include waiting. Nested scopes overlap and must not be added. "
            "Model ID 4294967295 denotes eager tasks in this runtime; other IDs include target graphs. "
            "Target graph kernels are missing from CPU-scope device associations in this runtime. "
            "Target attribution combines kernel-model-ID totals for graphs with eager target scope totals, "
            "validated against 36 FIA kernels per actual replay."
        ),
        "configurations": items,
    }
    (args.output / "profile_summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    export_scope_csv(items, args.output / "profile_scopes.csv")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
