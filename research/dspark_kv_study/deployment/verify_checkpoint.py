"""Read-only validation of an existing checkpoint against a frozen catalog."""

import argparse
import json
from pathlib import Path, PurePosixPath

from download_models import atomic_json, sha256


def verify(manifest_path, model, checkpoint, output):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = [entry for entry in manifest["files"] if entry["model"] == model]
    if not entries:
        raise ValueError("the frozen manifest does not contain this model")
    root = checkpoint.resolve()
    records = []
    for entry in entries:
        relative = PurePosixPath(entry["path"])
        if relative.parts[0] != model.split("/", 1)[1] or ".." in relative.parts:
            raise ValueError("invalid manifest path")
        target = root.joinpath(*relative.parts[1:]).resolve()
        if not target.is_relative_to(root):
            raise ValueError("checkpoint file escapes its directory")
        size = target.stat().st_size
        checksum = sha256(target)
        if size != entry["bytes"] or checksum != entry["sha256"]:
            raise ValueError(f"existing checkpoint differs from the pinned catalog: {relative.name}")
        records.append({"file": str(relative), "bytes": size, "sha256": checksum, "revision": entry["revision"]})
    report = {"model": model, "checkpoint": str(root), "all_files_verified": True, "files": records}
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(output, report)
    print(json.dumps({"model": model, "verified_files": len(records)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.manifest, args.model, args.checkpoint, args.output)
