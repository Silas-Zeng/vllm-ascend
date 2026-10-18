"""Create serial CPU-only SDK parsing steps after the entire NPU profile job ends."""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path


def main(args):
    root = args.root.resolve()
    state = json.loads((root / "state" / (args.profile_name + "_result.json")).read_text())
    if state["status"] != "complete" or len(state["steps"]) != 18 or any(s["exit_code"] for s in state["steps"]):
        raise ValueError("NPU profiles are not all terminal and successful")
    spec_path = root / "state" / (args.name + "_spec.json")
    if spec_path.exists():
        raise ValueError("analysis job already exists")
    helper = Path(__file__).with_name("analyze_one_native_profile.py")
    steps = []
    for batch in (1, 4):
        for context in (4096, 16384, 24576):
            for method in ("ar", "full", "window"):
                label = f"{method}_c{context}_b{batch}"
                captures = list((args.profile_directory / "traces" / label).glob("*_ascend_pt"))
                if len(captures) != 1:
                    raise ValueError("missing/ambiguous SDK capture: " + label)
                steps.append(
                    {
                        "name": label + "_parse",
                        "timeout_seconds": 600,
                        "argv": [
                            "bash",
                            str(root / "study/deployment/run_native.sh"),
                            str(root),
                            "python",
                            str(helper),
                            str(captures[0]),
                        ],
                    }
                )
    sources = [
        Path(__file__).resolve(),
        helper,
        *[root / "study/deployment" / name for name in ("run_job.py", "download_models.py", "run_native.sh")],
    ]
    spec = {
        "name": args.name,
        "root": str(root),
        "continue_after_failure": True,
        "scope": "serial native CPU SDK analysis after NPU measurements and profiling are terminal",
        "source_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
        "steps": steps,
    }
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile-name", required=True)
    parser.add_argument("--profile-directory", type=Path, required=True)
    parser.add_argument("--name", required=True)
    main(parser.parse_args())
