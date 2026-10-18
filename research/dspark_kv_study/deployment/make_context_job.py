"""Freeze and launch the approved 18-configuration context study, serially."""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

CONTEXTS = (4096, 16384, 24576)
CONCURRENCIES = (1, 4)
METHODS = ("ar", "full", "window")
SCHEDULED_TOKENS = 4096
EXTRA_DRAFT_SLOTS = 6


def build_steps(
    root,
    stage,
    window_runtime,
    output,
    phase,
    reference_directory,
    timing_method="emission",
    record_control_mismatch=False,
):
    """Generate native commands; comparison policy changes only post-generation handling."""
    steps = []
    for concurrency in CONCURRENCIES:
        for context in CONTEXTS:
            for method in METHODS:
                label = f"{method}_c{context}_b{concurrency}"
                prefix = str(stage) + (":" + str(window_runtime) if method == "window" else "")
                argv = [
                    "bash",
                    str(root / "study/deployment/run_native.sh"),
                    str(root),
                    "env",
                    "TASK_QUEUE_ENABLE=1",
                    "bash",
                    str(stage / "run_study_stage.sh"),
                    prefix,
                    "python",
                    str(stage / "run_vllm_baseline.py"),
                    "--mode",
                    method,
                    "--target",
                    "/model/Qwen3-8B",
                    "--prompts",
                    str(root / f"prompts/long/prompts_long_c{context}_b{concurrency}.json"),
                    "--output",
                    str(output / f"{label}.json"),
                    "--concurrency",
                    str(concurrency),
                    "--output-tokens",
                    "512",
                    "--max-model-len",
                    "32768",
                    "--max-num-batched-tokens",
                    str(SCHEDULED_TOKENS + (EXTRA_DRAFT_SLOTS * concurrency if method != "ar" else 0)),
                    "--max-num-scheduled-tokens",
                    str(SCHEDULED_TOKENS),
                    "--chunked-prefill",
                    "--async-scheduling",
                    "--timing-method",
                    timing_method,
                    "--compare-generate",
                    "--warmup",
                    "10" if phase == "timing" else "3",
                    "--repeats",
                    "10" if phase == "timing" else "1",
                ]
                if method != "ar":
                    argv += ["--draft", str(root / "models/dspark_qwen3_8b_block7")]
                if method != "ar" or phase == "profile":
                    directory = output if phase == "timing" else reference_directory
                    argv += [
                        "--reference",
                        str(directory / f"ar_c{context}_b{concurrency}.json"),
                        "--record-reference-mismatch",
                    ]
                if phase == "profile":
                    argv += ["--profile-dir", str(output / "traces" / label), "--profile-max-iterations", "64"]
                    if timing_method == "native_stats":
                        argv += ["--record-control-mismatch"]
                else:
                    if record_control_mismatch:
                        argv += ["--record-control-mismatch"]
                    if method != "ar":
                        argv += [
                            "--audit-dir",
                            str(output / "audits" / label),
                            "--audit-after",
                            "--audit-output-tokens",
                            "32",
                        ]
                steps.append({"name": label + "_measure", "timeout_seconds": 1800, "argv": argv})
                if phase == "timing" and method != "ar":
                    steps.append(
                        {
                            "name": label + "_audit",
                            "timeout_seconds": 120,
                            "argv": [
                                "python3",
                                str(root / "study/deployment/verify_live_audit.py"),
                                str(stage),
                                str(output / "audits" / label),
                                str(output / f"{label}_audit.json"),
                            ],
                        }
                    )
    return steps


def main(args):
    root = args.root.resolve()
    stage = args.stage.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    spec_path = root / "state" / (args.name + "_spec.json")
    if spec_path.exists():
        raise ValueError("job spec already exists; inspect its state instead of relaunching")
    steps = build_steps(
        root,
        stage,
        args.window_runtime.resolve(),
        output,
        args.phase,
        args.reference_directory,
        args.timing_method,
        args.record_control_mismatch,
    )
    sources = [path for path in stage.iterdir() if path.is_file()]
    sources += [
        root / "study/deployment" / name
        for name in ["run_job.py", "download_models.py", "run_native.sh", "verify_live_audit.py"]
    ]
    sources.append(Path(__file__).resolve())
    sources += [
        root / f"prompts/long/prompts_long_c{context}_b{batch}.json" for batch in CONCURRENCIES for context in CONTEXTS
    ]
    sources += [
        root / "runtime/vllm_ascend/spec_decode/llm_base_proposer.py",
        root / "runtime/vllm_ascend/spec_decode/utils.py",
        root / "runtime/vllm_ascend/compilation/acl_graph.py",
        root / "runtime/vllm_ascend/attention/attention_v1.py",
        args.window_runtime / "vllm_ascend/spec_decode/llm_base_proposer.py",
        Path("/model/Qwen3-8B/config.json"),
        root / "models/dspark_qwen3_8b_block7/config.json",
    ]
    sources += [
        Path("/vllm-workspace/vllm/vllm") / name
        for name in (
            "entrypoints/offline_utils.py",
            "v1/engine/output_processor.py",
            "v1/engine/llm_engine.py",
            "v1/engine/__init__.py",
            "v1/metrics/stats.py",
        )
    ]
    if args.phase == "profile":
        sources += [
            args.reference_directory / f"ar_c{context}_b{batch}.json" for batch in CONCURRENCIES for context in CONTEXTS
        ]
    spec = {
        "name": args.name,
        "root": str(root),
        "continue_after_failure": True,
        "scope": "approved controlled context matrix"
        if args.phase == "timing"
        else "separate 64-worker-iteration NPU profiling",
        "configuration_count": 18,
        "phase": args.phase,
        "source_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
        "steps": steps,
    }
    spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    if args.launch:
        log = (root / "logs" / (args.name + "_supervisor.log")).open("ab", buffering=0)
        process = subprocess.Popen(
            ["python3", str(root / "study/deployment/run_job.py"), str(spec_path)],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        launch = {
            "pid": process.pid,
            "startticks": Path(f"/proc/{process.pid}/stat").read_text().split()[21],
            "started_at": time.time(),
        }
        (root / "state" / (args.name + "_launch.json")).write_text(json.dumps(launch, indent=2) + "\n")
        print(json.dumps(launch))
    print(json.dumps({"spec": str(spec_path), "steps": len(steps), "configurations": 18}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--window-runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--phase", choices=["timing", "profile"], required=True)
    parser.add_argument("--reference-directory", type=Path)
    parser.add_argument("--timing-method", choices=["emission", "native_core", "native_stats"], default="native_stats")
    parser.add_argument("--record-control-mismatch", action="store_true")
    parser.add_argument("--launch", action="store_true")
    args = parser.parse_args()
    if args.record_control_mismatch and args.timing_method != "native_stats":
        parser.error("record-control-mismatch requires native_stats")
    main(args)
