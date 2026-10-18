import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from deployment.run_proot_runtime import invocation


class RuntimeIsolationTests(unittest.TestCase):
    def prepare(self, root):
        for relative in ("bundle/rootfs/bin", "oci/blobs/sha256", "state"):
            (root / relative).mkdir(parents=True)
        (root / "bundle/rootfs/bin/bash").touch()
        config = {
            "config": {
                "Env": ["PATH=/usr/bin", "PYTHONPATH=/guest"],
                "Entrypoint": ["/bin/bash", "-c", 'exec "$@"', "--"],
            }
        }
        raw = json.dumps(config).encode()
        digest = hashlib.sha256(raw).hexdigest()
        (root / "oci/blobs/sha256" / digest).write_bytes(raw)
        (root / "runtime_manifest.json").write_text(
            json.dumps({"manifest": {"config": {"digest": "sha256:" + digest}}})
        )
        (root / "state/runtime_pull.json").write_text(json.dumps({"status": "complete"}))
        args = argparse.Namespace(
            root=root, study=root / "study", proot=root / "proot", env=[], command=["--", "python", "check.py"]
        )
        return args, digest

    def test_verified_config_and_explicit_guest_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.prepare(Path(directory))
            command = invocation(args)
            self.assertIn("-i", command)
            self.assertIn("PYTHONPATH=" + str(args.study.resolve() / "study") + ":/guest", command)
            self.assertEqual(command[-6:], ["/bin/bash", "-c", 'exec "$@"', "--", "python", "check.py"])

    def test_incomplete_or_changed_runtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args, digest = self.prepare(root)
            (root / "state/runtime_pull.json").write_text(json.dumps({"status": "running"}))
            with self.assertRaises(ValueError):
                invocation(args)
            (root / "state/runtime_pull.json").write_text(json.dumps({"status": "complete"}))
            (root / "oci/blobs/sha256" / digest).write_bytes(b"changed")
            with self.assertRaises(ValueError):
                invocation(args)

    def test_cross_hardware_resume_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime_manifest.json").write_text(
                json.dumps({"image": "quay.io/ascend/vllm-ascend:v0.26.0rc1-a3"})
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).parent / "deployment/pull_runtime.py"),
                    "--root",
                    str(root),
                    "--tag",
                    "v0.26.0rc1",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("different hardware image", result.stderr)


if __name__ == "__main__":
    unittest.main()
