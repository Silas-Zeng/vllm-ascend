"""Summarize frozen diagnostic evidence; this is not the formal length matrix."""

import argparse
import gzip
import hashlib
import json
import math
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

NATIVE_REPORTS = (
    "ar_queue1.json",
    "ar_unset_before.json",
    "ar_queue1_repeat.json",
    "ar_unset_after.json",
    "dspark_queue1.json",
    "ar_cold_unset_1.json",
    "ar_cold_queue1.json",
    "ar_cold_unset_2.json",
    "dspark_cold_queue1.json",
    "ar_stability_0.json",
    "dspark_stability_0.json",
    "ar_stability_1.json",
    "dspark_stability_1.json",
)
PROMPT_SHA256 = "9cf2e712e05e220e097d02fe8630d869423124bfc16d7020a5950a27fc78c8ff"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def verify_archive(directory):
    manifest = read_json(directory / "archive_manifest.json")
    mapping = read_json(directory / "local_path_map.json")
    names = [entry["file"] for entry in manifest]
    if len(names) != len(set(names)) or set(mapping) != set(names):
        raise ValueError("archive manifest and local path map disagree")
    for entry in manifest:
        path = (directory / mapping[entry["file"]]).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("mapped path leaves evidence directory")
        raw = path.read_bytes()
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"archive checksum mismatch: {entry['file']}")
        if "uncompressed_sha256" in entry:
            expanded = gzip.decompress(raw)
            if (
                len(expanded) != entry["uncompressed_bytes"]
                or hashlib.sha256(expanded).hexdigest() != entry["uncompressed_sha256"]
            ):
                raise ValueError(f"expanded checksum mismatch: {entry['file']}")
    return len(manifest)


def report_row(filename, report, reference):
    expected = {
        "prompt_sha256": PROMPT_SHA256,
        "prompt_lengths": [1024],
        "output_tokens": 512,
        "concurrency": 1,
        "seed": 42,
        "stop_at_eos": False,
        "timing_method": "generate",
        "target_eager": False,
        "disable_qknorm_rope": False,
        "history_config": None,
        "runner_sha256": "ecceb1f3fe893d5390140cc3619d9afa266a50f2fe1af0c8e03d843b094c47ea",
        "proposer_sha256": "25f0e85ea44291448b8a1b91408d6e4f8c80636646b603f78dd1c3a8fdece01f",
    }
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError(f"diagnostic configuration mismatch: {filename}")
    mode = "ar" if filename.startswith("ar_") else "full"
    expected_repeats = 10 if "stability" in filename else 2
    if report["mode"] != mode or len(report["runs"]) != expected_repeats:
        raise ValueError(f"mode or repeat count mismatch: {filename}")
    times = []
    for index, run in enumerate(report["runs"]):
        ids = run["output_token_ids"]
        if len(ids) != 1 or len(ids[0]) != 512 or run["iteration"] != index:
            raise ValueError(f"incomplete output or iteration sequence: {filename}")
        if reference is None:
            reference = ids[0]
        if ids[0] != reference:
            raise ValueError(f"greedy output differs: {filename}, iteration {index}")
        if run["matches_ar_reference"] is False:
            raise ValueError(f"runner reports reference mismatch: {filename}")
        if mode == "full" and "stability" in filename and run["matches_ar_reference"] is not True:
            raise ValueError(f"predeclared AR reference comparison missing: {filename}")
        if not math.isfinite(run["wall_seconds"]) or run["wall_seconds"] <= 0:
            raise ValueError(f"invalid duration: {filename}")
        if mode == "full":
            counters = run["speculative_counter_deltas"]
            drafts = counters["vllm:spec_decode_num_drafts"]
            proposed = counters["vllm:spec_decode_num_draft_tokens"]
            accepted = counters["vllm:spec_decode_num_accepted_tokens"]
            if drafts <= 0 or proposed != 7 * drafts or not 0 <= accepted <= proposed:
                raise ValueError(f"inconsistent K=7 acceptance counters: {filename}")
        times.append(run["wall_seconds"])
    return {
        "file": filename,
        "mode": mode,
        "measured_runs": len(times),
        "wall_seconds_including_prefill": times,
        "median_seconds": statistics.median(times),
        "min_seconds": min(times),
        "max_seconds": max(times),
        "speculative_counter_deltas": [run["speculative_counter_deltas"] for run in report["runs"]],
    }, reference


def main(directory, figures):
    archive_count = verify_archive(directory)
    job = read_json(directory / "bounded_native_stability_20261018_result.json")
    if job["status"] != "complete" or len(job["steps"]) != 4 or any(s["exit_code"] for s in job["steps"]):
        raise ValueError("predeclared stability job did not complete all four steps")
    spec = read_json(directory / "bounded_native_stability_20261018_spec.json")
    if [step["name"] for step in spec["steps"]] != ["ar_0", "dspark_0", "ar_1", "dspark_1"]:
        raise ValueError("predeclared launch order changed")
    for step in spec["steps"]:
        argv = step["argv"]
        for option, expected in [("--warmup", "3"), ("--repeats", "10"), ("--timing-method", "generate")]:
            if option not in argv or argv[argv.index(option) + 1] != expected:
                raise ValueError(f"predeclared stability setting changed: {option}")
    rows, reference = [], None
    for filename in NATIVE_REPORTS:
        row, reference = report_row(filename, read_json(directory / filename), reference)
        rows.append(row)
    summary = {
        "scope": "single-input native runtime diagnosis; not a context-length benchmark",
        "verified_archive_files": archive_count,
        "analysis_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "prompt_sha256": PROMPT_SHA256,
        "input_tokens": 1024,
        "output_tokens_per_request": 512,
        "concurrency": 1,
        "native_processes": len(rows),
        "native_measured_outputs": sum(row["measured_runs"] for row in rows),
        "all_measured_tokens_identical": True,
        "output_token_sha256_json_compact": hashlib.sha256(
            json.dumps(reference, separators=(",", ":")).encode()
        ).hexdigest(),
        "request_tpot_ms": None,
        "processes": rows,
    }
    (directory / "derived_native_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.7), sharey=True)
    upper_limit = 1.12 * max(
        value for row in rows if "stability" in row["file"] for value in row["wall_seconds_including_prefill"]
    )
    for repeat, ax in enumerate(axes):
        for prefix, label, color in [("ar", "AR", "#3366a3"), ("dspark", "DSpark K=7", "#c76b25")]:
            row = next(row for row in rows if row["file"] == f"{prefix}_stability_{repeat}.json")
            times = row["wall_seconds_including_prefill"]
            ax.plot(
                range(1, 11),
                times,
                marker="o",
                ms=4,
                color=color,
                label=f"{label}: median {row['median_seconds']:.3f} s",
            )
        ax.set(title=f"Independent launch pair {repeat + 1}", xlabel="Measured repetition", xticks=range(1, 11))
        ax.set_ylim(0, upper_limit)
        ax.grid(alpha=0.2)
        ax.legend(loc="lower right", fontsize=9)
    axes[0].set_ylabel("Generation time including prefill (seconds)")
    fig.suptitle("Native single-input stability diagnosis: 1K input, 512 output, batch 1")
    fig.text(
        0.5,
        0.015,
        "BF16 | max_len=1544 | target graph + eager draft | 3 warmups, 10 measurements | formal matrix pending",
        ha="center",
        fontsize=9,
    )
    fig.tight_layout(rect=(0, 0.05, 1, 0.96))
    figures.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg"):
        fig.savefig(figures / f"native_single_input_recheck.{extension}", dpi=160)
    plt.close(fig)
    print(json.dumps({key: value for key, value in summary.items() if key != "processes"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence", type=Path, default=Path(__file__).parent / "evidence/server_b2/bounded_diagnosis_20261018"
    )
    parser.add_argument("--figures", type=Path, default=Path(__file__).parent / "figures")
    args = parser.parse_args()
    main(args.evidence, args.figures)
