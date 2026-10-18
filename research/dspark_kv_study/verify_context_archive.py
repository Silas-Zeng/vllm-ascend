"""Verify downloaded context evidence bytes, gzip payloads and terminal jobs."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath


def stream_digest(stream):
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
        size += len(block)
    return size, digest.hexdigest()


def verify_files(root, entries):
    root = root.resolve()
    seen = set()
    for entry in entries:
        relative = PurePosixPath(entry["destination"])
        path = root.joinpath(*relative.parts).resolve()
        if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(root):
            raise ValueError("manifest path leaves evidence directory")
        if path in seen:
            raise ValueError("duplicate evidence destination")
        seen.add(path)
        with path.open("rb") as stream:
            size, checksum = stream_digest(stream)
        if (size, checksum) != (entry["bytes"], entry["sha256"]):
            raise ValueError("downloaded checksum mismatch: " + str(relative))
        if entry["compression"] not in (None, "gzip"):
            raise ValueError("unknown evidence compression")
        opener = gzip.open if entry["compression"] == "gzip" else open
        with opener(path, "rb") as stream:
            size, checksum = stream_digest(stream)
        if (size, checksum) != (entry["raw_bytes"], entry["raw_sha256"]):
            raise ValueError("expanded checksum mismatch: " + str(relative))
    return len(seen)


def verify(root):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    count = verify_files(root, manifest["files"])
    jobs = [(manifest["timing_job"], manifest["timing_steps"]), (manifest["profile_job"], 18)]
    if manifest["analysis_job"]:
        jobs.append((manifest["analysis_job"], 18))
    for name, expected in jobs:
        state = json.loads((root / "state" / (name + "_result.json")).read_text(encoding="utf-8"))
        if (
            state["status"] != "complete"
            or len(state["steps"]) != expected
            or any(step["exit_code"] != 0 for step in state["steps"])
        ):
            raise ValueError("nonterminal or failed job: " + name)
    return {
        "downloaded_files_verified": count,
        "terminal_jobs_verified": len(jobs),
        "remote_sdk_files_in_inventory": len(manifest["remote_sdk_files"]),
        "remote_sdk_bytes_downloaded": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory), indent=2))
