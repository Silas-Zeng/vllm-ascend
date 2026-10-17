"""Pull a pinned official arm64 runtime into an OCI layout without Docker.

The optional transport mirror must return exactly the official manifest bytes.
Every config and layer is checked against the official content digest.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from download_models import atomic_json, sha256


class Registry:
    def __init__(self, base, repository):
        self.base = base.rstrip("/")
        self.repository = repository
        self.token = None
        self.accept = ", ".join(
            (
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            )
        )

    def open(self, path, offset=0):
        for attempt in range(2):
            headers = {"Accept": self.accept}
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = urllib.request.Request(self.base + "/v2/" + self.repository + "/" + path, headers=headers)
            try:
                return urllib.request.urlopen(request, timeout=90)
            except urllib.error.HTTPError as error:
                if error.code != 401 or attempt:
                    raise
                auth = dict(re.findall(r'(\w+)="([^"]*)"', error.headers.get("WWW-Authenticate", "")))
                endpoint = (
                    auth["realm"]
                    + "?"
                    + urllib.parse.urlencode({key: auth[key] for key in ("service", "scope") if key in auth})
                )
                if not endpoint.startswith("https://"):
                    raise ValueError("registry auth endpoint must use TLS") from error
                with urllib.request.urlopen(endpoint, timeout=30) as response:
                    credentials = json.load(response)
                self.token = credentials.get("token") or credentials["access_token"]
        raise RuntimeError("registry authentication failed")

    def document(self, reference):
        with self.open("manifests/" + reference) as response:
            return response.read()


def digest_bytes(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def run(root, transport, workers):
    official = Registry("https://quay.io", "ascend/vllm-ascend")
    mirror = Registry(transport, "ascend/vllm-ascend")
    plan_path = root / "runtime_manifest.json"
    tag = "v0.26.0rc1-a3"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        image = plan["image_digest"]
        raw_manifest = official.document(image)
    else:
        raw_index = official.document(tag)
        index = json.loads(raw_index)
        candidates = [
            descriptor
            for descriptor in index["manifests"]
            if descriptor.get("platform", {}).get("architecture") == "arm64"
            and descriptor.get("platform", {}).get("os") == "linux"
        ]
        if len(candidates) != 1:
            raise ValueError("expected one official Linux arm64 image")
        image = candidates[0]["digest"]
        raw_manifest = official.document(image)
        plan = {
            "image": "quay.io/ascend/vllm-ascend:" + tag,
            "index_digest": digest_bytes(raw_index),
            "image_digest": image,
            "transport": transport,
            "manifest": json.loads(raw_manifest),
        }
        atomic_json(plan_path, plan)
    if digest_bytes(raw_manifest) != image or mirror.document(image) != raw_manifest:
        raise ValueError("runtime transport does not match the pinned official image")
    layout = root / "oci"
    blobs = layout / "blobs/sha256"
    blobs.mkdir(parents=True, exist_ok=True)
    (blobs / image.split(":", 1)[1]).write_bytes(raw_manifest)
    atomic_json(layout / "oci-layout", {"imageLayoutVersion": "1.0.0"})
    atomic_json(
        layout / "index.json",
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": image,
                    "size": len(raw_manifest),
                    "annotations": {"org.opencontainers.image.ref.name": "dspark"},
                }
            ],
        },
    )
    descriptors = [plan["manifest"]["config"], *plan["manifest"]["layers"]]
    state_path = root / "state/runtime_pull.json"
    state = {"pid": os.getpid(), "status": "running", "image_digest": image, "blobs": {}}
    lock = threading.Lock()

    def update(digest, **values):
        with lock:
            state["blobs"].setdefault(digest, {}).update(values)
            atomic_json(state_path, state)

    def download(descriptor):
        digest = descriptor["digest"]
        algorithm, hex_digest = digest.split(":", 1)
        if algorithm != "sha256" or not re.fullmatch(r"[a-f0-9]{64}", hex_digest):
            raise ValueError("unsupported image content digest")
        target = blobs / hex_digest
        if target.exists() and target.stat().st_size == descriptor["size"] and sha256(target) == hex_digest:
            update(digest, status="verified", downloaded_bytes=descriptor["size"])
            return
        partial = target.with_suffix(".part")
        for attempt in range(4):
            try:
                offset = partial.stat().st_size if partial.exists() else 0
                update(digest, status="downloading", downloaded_bytes=offset, expected_bytes=descriptor["size"])
                with mirror.open("blobs/" + digest, offset) as response:
                    mode = "ab" if offset and response.status == 206 else "wb"
                    if mode == "ab" and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                        raise ValueError("incorrect runtime blob range")
                    if mode == "wb":
                        offset = 0
                    last_update = time.monotonic()
                    with partial.open(mode) as stream:
                        while block := response.read(4 * 1024 * 1024):
                            stream.write(block)
                            offset += len(block)
                            if time.monotonic() - last_update > 2:
                                update(digest, downloaded_bytes=offset)
                                last_update = time.monotonic()
                if partial.stat().st_size != descriptor["size"] or sha256(partial) != hex_digest:
                    raise ValueError("runtime blob size or SHA-256 mismatch")
                partial.replace(target)
                update(digest, status="verified", downloaded_bytes=descriptor["size"])
                print(json.dumps({"verified_blob": digest, "bytes": descriptor["size"]}), flush=True)
                return
            except Exception as error:
                update(digest, status="retrying", attempt=attempt + 1, error=str(error))
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))

    atomic_json(state_path, state)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(download, descriptors))
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
    parser.add_argument("--transport", default="https://quay.nju.edu.cn")
    parser.add_argument("--workers", type=int, default=3, choices=range(1, 5))
    arguments = parser.parse_args()
    run(arguments.root, arguments.transport, arguments.workers)
