"""Download pinned ModelScope files, checking every recorded SHA-256.

Weights stay on the experiment server. Only the bounded manifest belongs in git.
Reruns reuse the first manifest instead of silently following a changed branch.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


def confined_path(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError("model file path escapes its destination")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("model file resolves outside its destination")
    return resolved


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def freeze_manifest(path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    models = ("Qwen/Qwen3-8B", "deepseek-ai/dspark_qwen3_8b_block7")
    manifest = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "source": "ModelScope", "files": []}
    for model in models:
        api = f"https://modelscope.cn/api/v1/models/{model}/repo/files?Revision=master&Recursive=true"
        with urllib.request.urlopen(api, timeout=30) as response:
            catalog = json.load(response)
        if not catalog.get("Success"):
            raise ValueError(f"model catalog failed: {model}")
        for item in catalog["Data"]["Files"]:
            if item["Type"] != "blob":
                continue
            relative = item["Path"]
            if len(item.get("Sha256", "")) != 64 or len(item.get("Revision", "")) != 40:
                raise ValueError("model catalog lacks a pinned revision or content checksum")
            encoded = urllib.parse.quote(relative, safe="/")
            manifest["files"].append(
                {
                    "model": model,
                    "path": model.split("/", 1)[1] + "/" + relative,
                    "url": f"https://modelscope.cn/models/{model}/resolve/{item['Revision']}/{encoded}",
                    "revision": item["Revision"],
                    "bytes": item["Size"],
                    "sha256": item["Sha256"],
                }
            )
    manifest["files"].sort(key=lambda entry: (entry["path"].endswith(".safetensors"), entry["path"]))
    atomic_json(path, manifest)
    return manifest


def run(root, workers, selected_models=None):
    root.mkdir(parents=True, exist_ok=True)
    manifest = freeze_manifest(root / "model_manifest.json")
    files = [entry for entry in manifest["files"] if not selected_models or entry["model"] in selected_models]
    if not files:
        raise ValueError("the frozen manifest contains none of the selected models")
    destination = root / "models"
    destination.mkdir(exist_ok=True)
    required = sum(entry["bytes"] for entry in files)
    if shutil.disk_usage(root).free < required + 20 * 1024**3:
        raise RuntimeError("insufficient free disk for pinned models plus runtime headroom")
    state_path = root / "state" / "model_download.json"
    state_path.parent.mkdir(exist_ok=True)
    state = {
        "pid": os.getpid(), "status": "running", "files": {}, "total_bytes": required,
        "selected_models": sorted(selected_models) if selected_models else None,
    }
    lock = threading.Lock()

    def update(name, **values):
        with lock:
            state["files"].setdefault(name, {}).update(values)
            state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            atomic_json(state_path, state)

    def download(entry):
        target = confined_path(destination, entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == entry["bytes"] and sha256(target) == entry["sha256"]:
            update(entry["path"], status="verified", downloaded_bytes=entry["bytes"])
            return
        partial = target.with_name(target.name + ".part")
        update(entry["path"], status="downloading", downloaded_bytes=partial.stat().st_size if partial.exists() else 0)
        for attempt in range(4):
            try:
                offset = partial.stat().st_size if partial.exists() else 0
                headers = {"User-Agent": "dspark-kv-study/1.0"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                request = urllib.request.Request(entry["url"], headers=headers)
                with urllib.request.urlopen(request, timeout=90) as response:
                    if offset and response.status == 206:
                        if not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                            raise ValueError("incorrect range response")
                        mode = "ab"
                    else:
                        offset, mode = 0, "wb"
                    last_update = time.monotonic()
                    with partial.open(mode) as stream:
                        while block := response.read(4 * 1024 * 1024):
                            stream.write(block)
                            offset += len(block)
                            if time.monotonic() - last_update > 2:
                                update(entry["path"], downloaded_bytes=offset)
                                last_update = time.monotonic()
                if partial.stat().st_size != entry["bytes"] or sha256(partial) != entry["sha256"]:
                    raise ValueError("downloaded size or SHA-256 does not match frozen catalog")
                partial.replace(target)
                update(entry["path"], status="verified", downloaded_bytes=entry["bytes"], sha256=entry["sha256"])
                print(json.dumps({"verified": entry["path"], "bytes": entry["bytes"]}), flush=True)
                return
            except Exception as error:
                update(entry["path"], status="retrying", attempt=attempt + 1, error=str(error))
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))

    atomic_json(state_path, state)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(download, files))
        state["status"] = "complete"
    except Exception:
        state["status"] = "failed"
        raise
    finally:
        with lock:
            atomic_json(state_path, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--model", action="append", choices=["Qwen/Qwen3-8B", "deepseek-ai/dspark_qwen3_8b_block7"])
    args = parser.parse_args()
    run(args.root, args.workers, args.model)
