"""Rebuild tables and scientific plots from the sealed downloaded evidence."""

import argparse
import gzip
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from analyze_context_matrix import analyze, plot, validate_report, write_csv
from analyze_context_profiles import export_scope_csv, summarize
from render_context_tables import macro_tables, profile_tables, request_tables
from verify_context_archive import verify


def main(args):
    """Validate the seal, rebuild all statistics, and keep instrumented timings separate."""
    print(json.dumps(verify(args.evidence)), flush=True)
    from tokenizers import Tokenizer

    catalog = json.loads((args.evidence / "model_manifest.json").read_text(encoding="utf-8"))
    expected = next(
        item
        for item in catalog["files"]
        if item["model"] == "Qwen/Qwen3-8B" and item["path"].endswith("/tokenizer.json")
    )
    tokenizer_sha = hashlib.sha256(args.tokenizer.read_bytes()).hexdigest()
    if tokenizer_sha != expected["sha256"] or args.tokenizer.stat().st_size != expected["bytes"]:
        raise ValueError("tokenizer differs from the frozen model catalog")
    result = analyze(
        args.evidence / "formal_native", args.evidence / "prompts", Tokenizer.from_file(str(args.tokenizer))
    )
    result["tokenizer_sha256"] = tokenizer_sha
    args.output.mkdir(parents=True, exist_ok=True)
    args.figures.mkdir(parents=True, exist_ok=True)
    (args.output / "context_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_csv(result, args.output / "context_summary.csv")
    plot(result, args.figures)
    profiles = []
    runtime_observations = []
    with tempfile.TemporaryDirectory(prefix="dspark-context-csv-") as temporary:
        folder = Path(temporary)
        for item in result["configurations"]:
            label = f"{item['method']}_c{item['context']}_b{item['concurrency']}"
            for name in ("operator_details.csv", "kernel_details.csv", "step_trace_time.csv"):
                with (
                    gzip.open(args.evidence / "profiles" / label / (name + ".gz"), "rb") as source,
                    (folder / name).open("wb") as target,
                ):
                    shutil.copyfileobj(source, target)
            profiles.append(summarize(label, folder))
            report = json.loads((args.evidence / "profile_runs" / (label + ".json")).read_text(encoding="utf-8"))
            validate_report(report, item["method"], item["context"], item["concurrency"], "native_stats", profiled=True)
            formal = json.loads((args.evidence / "formal_native" / (label + ".json")).read_text(encoding="utf-8"))
            for key in (
                "runner_sha256",
                "proposer_sha256",
                "target_config_sha256",
                "draft_config_sha256",
                "prompt_sha256",
                "packages",
            ):
                if report[key] != formal[key]:
                    raise ValueError("profile/formal source or workload drift: " + label + " / " + key)
            ar = json.loads(
                (args.evidence / "formal_native" / f"ar_c{item['context']}_b{item['concurrency']}.json").read_text()
            )
            run = report["runs"][0]
            exact = [tokens == ar["runs"][0]["output_token_ids"][i] for i, tokens in enumerate(run["output_token_ids"])]
            if run["matches_ar_reference"] != all(exact):
                raise ValueError("profile AR comparison flag contradicts actual tokens: " + label)
            runtime_observations.append(
                {
                    "label": label,
                    "wall_seconds": run["wall_seconds"],
                    "wall_vs_formal_median": run["wall_seconds"] / item["wall_seconds"]["median"],
                    "matches_native_control": run["matches_generate_control"],
                    "ar_exact_requests": sum(exact),
                    "requests": item["concurrency"],
                }
            )
    (args.output / "profile_summary.json").write_text(
        json.dumps({"scope": "separate first-64-worker-iteration profiles", "configurations": profiles}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    export_scope_csv(profiles, args.output / "profile_scopes.csv")
    (args.output / "profile_runtime_observations.json").write_text(
        json.dumps(
            {
                "scope": (
                    "one separate instrumented run per configuration; "
                    "warmup count and instrumentation differ from formal runs"
                ),
                "interpretation": (
                    "wall ratios show observed differences, not isolated profiler overhead or production speed"
                ),
                "configurations": runtime_observations,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (args.output / "context_tables.md").write_text(macro_tables(result["configurations"]), encoding="utf-8")
    requests, outputs = request_tables(result["configurations"])
    (args.output / "context_request_details.md").write_text(requests, encoding="utf-8")
    (args.output / "context_outputs.md").write_text(outputs, encoding="utf-8")
    (args.output / "context_profile_tables.md").write_text(profile_tables(profiles), encoding="utf-8")
    from plot_context_profiles import plot_stages, plot_timeline

    plot_stages(profiles, args.figures)
    attributions = {
        item["label"]: set(item["target_attribution"]["graph_models_with_target_attention"]) for item in profiles
    }
    for batch in (1, 4):
        labels = [f"{method}_c24576_b{batch}" for method in ("full", "window")]
        plot_timeline(args.evidence / "profiles", args.figures, labels, f"timeline_24k_c{batch}", attributions)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--figures", type=Path, required=True)
    main(parser.parse_args())
