"""Seal completed context study artifacts, preserving raw and compressed hashes."""

import argparse
import gzip
import hashlib
import json
import shutil
from decimal import Decimal
from pathlib import Path


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def pack(source, destination, compress=False):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("archive destination already exists: " + str(destination))
    if compress:
        with (
            source.open("rb") as incoming,
            destination.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as outgoing,
        ):
            shutil.copyfileobj(incoming, outgoing)
    else:
        shutil.copyfile(source, destination)
    return {
        "source": str(source),
        "raw_sha256": digest(source),
        "raw_bytes": source.stat().st_size,
        "sha256": digest(destination),
        "bytes": destination.stat().st_size,
        "compression": "gzip" if compress else None,
    }


def compact_trace(source, destination):
    trace = json.loads(source.read_text(), parse_float=Decimal)
    events = trace["traceEvents"] if isinstance(trace, dict) else trace
    # Native TorchNPU exports hardware events without a category. Identify them
    # by actual task metadata rather than guessing from kernel names.
    selected = [
        event
        for event in events
        if event.get("ph") == "M"
        or (
            event.get("ph") == "X"
            and (event.get("name", "").startswith("study/") or "Task Type" in event.get("args", {}))
        )
    ]
    if not any("Task Type" in event.get("args", {}) for event in selected):
        raise ValueError("no native hardware events found in " + str(source))
    result = {
        "traceEvents": selected,
        "study_provenance": {
            "source_sha256": digest(source),
            "source_event_count": len(events),
            "selected_event_count": len(selected),
            "filter": (
                "all metadata, study CPU scopes, and native hardware complete events; "
                "original values retained; JSON fractional numbers represented as exact decimal strings"
            ),
        },
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(result, separators=(",", ":"), default=str) + "\n").encode()
    with destination.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
        stream.write(payload)
    return {
        "source": str(source),
        "source_sha256": digest(source),
        "sha256": digest(destination),
        "bytes": destination.stat().st_size,
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "raw_bytes": len(payload),
        "compression": "gzip",
        "derived": True,
    }


def main(args):
    """Seal terminal jobs and byte-preserving evidence, including failed earlier attempts."""
    root = args.root.resolve()
    evidence = root / "evidence/context_baseline_20261018"
    final_jobs = [args.timing_name, args.profile_name]
    for name, count in zip(final_jobs, (args.timing_steps, 18)):
        state = json.loads((root / "state" / (name + "_result.json")).read_text())
        if state["status"] != "complete" or len(state["steps"]) != count or any(s["exit_code"] for s in state["steps"]):
            raise ValueError("cannot seal incomplete/failed final job: " + name)
    if args.analysis_name:
        state = json.loads((root / "state" / (args.analysis_name + "_result.json")).read_text())
        if state["status"] != "complete" or len(state["steps"]) != 18 or any(s["exit_code"] for s in state["steps"]):
            raise ValueError("cannot seal incomplete/failed SDK analysis")
    args.output.mkdir(parents=True, exist_ok=False)
    files = []
    destinations = {}

    def add(source, relative, compress=False):
        destination = args.output / relative
        if str(relative) in destinations:
            if destinations[str(relative)] != digest(source):
                raise ValueError("different content mapped to same archive name")
            return
        item = pack(source, destination, compress)
        item["destination"] = str(relative)
        files.append(item)
        destinations[str(relative)] = item["raw_sha256"]

    jobs = [
        "context_gate_ar_v1_20261018",
        "context_gate_ar_v2_20261018",
        "context_gate_ar_v3_20261018",
        "context_gate_dspark_v1_20261018",
        "context_aligned_gates_v1_20261018",
        "context_formal_v1_20261018",
        "context_formal_completion_v2_20261018",
        "context_native_timing_probe_v1_20261018",
        "context_native_core_gate_v1_20261018",
        "context_native_unobserved_repeat_v1_20261018",
        "context_native_stats_gate_v1_20261018",
        "context_formal_native_stats_v7_20261018",
        *final_jobs,
    ]
    collector = Path(__file__).resolve()
    add(collector, Path("sources") / (digest(collector)[:12] + "_" + collector.name + ".gz"), True)
    if args.analysis_name:
        jobs.append(args.analysis_name)
    for name in jobs:
        for path in sorted((root / "state").glob(name + "*.json")):
            add(path, Path("state") / path.name)
        for path in sorted((root / "logs").glob(name + "*.log")):
            add(path, Path("logs") / (path.name + ".gz"), True)
        spec = json.loads((root / "state" / (name + "_spec.json")).read_text())
        for source, expected in spec["source_sha256"].items():
            snapshot = root / "source_snapshots" / name / expected / Path(source).name
            if digest(snapshot) != expected:
                raise ValueError("invalid frozen source snapshot: " + str(snapshot))
            add(snapshot, Path("sources") / (expected[:12] + "_" + Path(source).name + ".gz"), True)
    for name in (
        "context_profile_v4_20261018",
        "context_profile_v5_20261018",
        "context_profile_native_stats_v7_20261018",
    ):
        for path in sorted((root / "state").glob(name + "*_queue*.json")):
            add(path, Path("state") / path.name)
    for path in (root / "study/deployment").glob("queue_context_profiles*.py"):
        add(path, Path("sources") / (digest(path)[:12] + "_" + path.name + ".gz"), True)
    for path in (root / "prompts/long").glob("*.json"):
        add(path, Path("prompts") / path.name)
    add(root / "model_manifest.json", Path("model_manifest.json"))
    for path in sorted(evidence.iterdir()):
        if path.is_file() and not path.name.startswith("completed_checkpoint"):
            add(path, Path("gates") / path.name)
        elif path.is_dir() and path.name.startswith("gate_") and path.name.endswith("_reads"):
            for audit in sorted(path.glob("*.jsonl")):
                add(audit, Path("gates") / path.name / (audit.name + ".gz"), True)
    directories = [
        (evidence / "formal_v1", "legacy_formal"),
        (evidence / "formal_completed_v2", "legacy_completion"),
        (evidence / "formal_native_stats_v7", "strict_native_attempt"),
        (args.timing_directory.resolve(), "formal_native"),
    ]
    for directory, label in directories:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                relative = Path(label) / path.relative_to(directory)
                add(path, Path(str(relative) + ".gz") if path.suffix == ".jsonl" else relative, path.suffix == ".jsonl")
    profile = args.profile_directory.resolve()
    for path in sorted(profile.glob("*.json")):
        add(path, Path("profile_runs") / path.name)
    full_trace_labels = {
        "ar_c24576_b4",
        "full_c4096_b1",
        "window_c4096_b1",
        "full_c24576_b1",
        "window_c24576_b1",
        "full_c24576_b4",
        "window_c24576_b4",
    }
    sdk = []
    for label in sorted(path.name for path in (profile / "traces").iterdir() if path.is_dir()):
        print(json.dumps({"phase": "archive_profile", "label": label}), flush=True)
        candidates = list((profile / "traces" / label).glob("*_ascend_pt/ASCEND_PROFILER_OUTPUT"))
        if len(candidates) != 1:
            raise ValueError("missing/ambiguous parsed SDK profile: " + label)
        folder = candidates[0]
        for name in (
            "operator_details.csv",
            "kernel_details.csv",
            "op_statistic.csv",
            "api_statistic.csv",
            "step_trace_time.csv",
        ):
            add(folder / name, Path("profiles") / label / (name + ".gz"), True)
        trace_path = folder / "trace_view.json"
        relative = Path("profiles") / label / "compact.trace.json.gz"
        item = compact_trace(trace_path, args.output / relative)
        item["destination"] = str(relative)
        files.append(item)
        if label in full_trace_labels:
            add(trace_path, Path("profiles") / label / "raw.trace.json.gz", True)
        for path in sorted(folder.parent.rglob("*")):
            if path.is_file():
                sdk.append({"source": str(path), "bytes": path.stat().st_size, "sha256": digest(path)})
    manifest = {
        "scope": "sealed terminal timing, audits, separate profiles, failed gate attempts and frozen sources",
        "timing_job": args.timing_name,
        "profile_job": args.profile_name,
        "analysis_job": args.analysis_name,
        "timing_steps": args.timing_steps,
        "files": files,
        "remote_sdk_files": sdk,
        "collector_sha256": digest(Path(__file__)),
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "archive": str(args.output),
                "files": len(files),
                "bytes": sum(item["bytes"] for item in files),
                "sdk_files_retained_on_server": len(sdk),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile-name", required=True)
    parser.add_argument("--timing-name", required=True)
    parser.add_argument("--timing-directory", type=Path, required=True)
    parser.add_argument("--timing-steps", type=int, default=30)
    parser.add_argument("--analysis-name")
    parser.add_argument("--profile-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
