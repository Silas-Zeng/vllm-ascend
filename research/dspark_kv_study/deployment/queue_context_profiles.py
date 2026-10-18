"""Start separate profiling only after all formal timing/audit steps pass."""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path


def ready(result, measurements=18, audits=12):
    steps = result.get("steps", [])
    return (
        result.get("status") == "complete"
        and len(steps) == measurements + audits
        and sum(step["name"].endswith("_measure") for step in steps) == measurements
        and sum(step["name"].endswith("_audit") for step in steps) == audits
        and all(step.get("exit_code") == 0 for step in steps)
    )


def main(args):
    state = args.root / "state" / (args.name + "_queue.json")
    info = {
        "status": "waiting_for_formal",
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "started_at": time.time(),
    }
    state.write_text(json.dumps(info, indent=2) + "\n")
    result_path = args.root / "state" / (args.formal_name + "_result.json")
    while True:
        result = json.loads(result_path.read_text())
        if result["status"] != "running":
            break
        time.sleep(15)
    if not ready(result, args.measurements, args.audits):
        info.update(
            status="not_launched",
            reason="formal timing/audit matrix has missing or failed steps",
            formal_status=result["status"],
        )
        state.write_text(json.dumps(info, indent=2) + "\n")
        raise RuntimeError(info["reason"])
    spec = json.loads((args.root / "state" / (args.formal_name + "_spec.json")).read_text())
    for path, expected in spec["source_sha256"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise RuntimeError("frozen formal source changed: " + path)
    reference_directory = args.reference_directory or args.root / "evidence/context_baseline_20261018/formal_v1"
    for batch in (1, 4):
        for context in (4096, 16384, 24576):
            for method in ("ar", "full", "window"):
                label = f"{method}_c{context}_b{batch}"
                report = json.loads((reference_directory / (label + ".json")).read_text())
                if report["timing_method"] != args.timing_method:
                    raise RuntimeError("selected report uses a different timing entry: " + label)
                if len(report["runs"]) != 10:
                    raise RuntimeError("incomplete selected formal report: " + label)
                for run in report["runs"]:
                    actual = run["output_token_ids"] == report["generate_control_output_token_ids"]
                    if run["matches_generate_control"] != actual:
                        raise RuntimeError("control comparison flag does not reflect actual tokens: " + label)
                    if any(len(tokens) != 512 for tokens in run["output_token_ids"]):
                        raise RuntimeError("wrong actual output length: " + label)
                if method != "ar":
                    audit = json.loads((reference_directory / (label + "_audit.json")).read_text())
                    if not audit["passed"] or not audit["checked_request_group_calls"]:
                        raise RuntimeError("missing/failed read audit: " + label)
    argv = [
        "python3",
        str(args.builder or args.root / "study/deployment/make_context_job.py"),
        "--root",
        str(args.root),
        "--stage",
        str(args.stage),
        "--window-runtime",
        str(args.window_runtime),
        "--output",
        str(args.output),
        "--name",
        args.name,
        "--phase",
        "profile",
        "--timing-method",
        args.timing_method,
        "--reference-directory",
        str(reference_directory),
        "--launch",
    ]
    output = subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT)
    info.update(status="launched", finished_at=time.time(), launcher_output=output)
    state.write_text(json.dumps(info, indent=2) + "\n")
    print(output, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--window-runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--formal-name", default="context_formal_v1_20261018")
    parser.add_argument("--measurements", type=int, default=18)
    parser.add_argument("--audits", type=int, default=12)
    parser.add_argument("--reference-directory", type=Path)
    parser.add_argument("--builder", type=Path)
    parser.add_argument("--timing-method", choices=["emission", "native_stats"], default="native_stats")
    main(parser.parse_args())
