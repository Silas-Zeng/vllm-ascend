"""Run a frozen, sequential experiment job and preserve its terminal exit code."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from download_models import atomic_json


def main(spec_path):
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    root = Path(spec["root"])
    state_path = root / "state" / (spec["name"] + "_result.json")
    state = {"pid": os.getpid(), "status": "running", "started_at": time.time(), "steps": []}
    atomic_json(state_path, state)
    try:
        for step in spec["steps"]:
            for filename, expected in spec["source_sha256"].items():
                source = Path(filename).read_bytes()
                if hashlib.sha256(source).hexdigest() != expected:
                    raise ValueError("experiment source changed after job freeze")
                snapshot = root / "source_snapshots" / spec["name"] / expected / Path(filename).name
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                if snapshot.exists() and snapshot.read_bytes() != source:
                    raise ValueError("source snapshot no longer matches its content digest")
                if not snapshot.exists():
                    snapshot.write_bytes(source)
            log_path = root / "logs" / (spec["name"] + "_" + step["name"] + ".log")
            record = {"name": step["name"], "started_at": time.time(), "log": str(log_path.relative_to(root))}
            state["active_step"] = step["name"]
            atomic_json(state_path, state)
            with log_path.open("ab", buffering=0) as log:
                proc = subprocess.Popen(
                    step["argv"],
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                state["child_pid"] = proc.pid
                state["child_startticks"] = Path(f"/proc/{proc.pid}/stat").read_text().split()[21]
                atomic_json(state_path, state)
                try:
                    record["exit_code"] = proc.wait(timeout=step.get("timeout_seconds", 900))
                except subprocess.TimeoutExpired:
                    record["timed_out"] = True
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        record["exit_code"] = proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        record["exit_code"] = proc.wait()
            record["finished_at"] = time.time()
            state["steps"].append(record)
            atomic_json(state_path, state)
            if record["exit_code"] and not spec.get("continue_after_failure", False):
                raise RuntimeError(f"experiment step failed: {step['name']}")
        state["status"] = "completed_with_failures" if any(step["exit_code"] for step in state["steps"]) else "complete"
    except Exception as error:
        state["status"] = "failed"
        state["error"] = str(error)
        raise
    finally:
        state["finished_at"] = time.time()
        atomic_json(state_path, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    main(parser.parse_args().spec)
