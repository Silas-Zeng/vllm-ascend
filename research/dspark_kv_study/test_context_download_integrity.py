"""Reject altered downloads and unsafe archive paths before analysis."""

import gzip
import hashlib
import tempfile
import unittest
from pathlib import Path

from verify_context_archive import verify_files


class ContextDownloadIntegrityTests(unittest.TestCase):
    def test_compressed_digest_does_not_replace_expanded_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = b"device trace with original timestamp\n"
            packed = gzip.compress(raw, mtime=0)
            (root / "trace.gz").write_bytes(packed)
            entry = {
                "destination": "trace.gz",
                "bytes": len(packed),
                "sha256": hashlib.sha256(packed).hexdigest(),
                "raw_bytes": len(raw),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "compression": "gzip",
            }
            self.assertEqual(verify_files(root, [entry]), 1)
            entry["raw_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "expanded checksum"):
                verify_files(root, [entry])

    def test_manifest_path_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            for destination in ("../outside", "/outside"):
                with self.assertRaisesRegex(ValueError, "leaves evidence"):
                    verify_files(Path(temporary), [{"destination": destination}])


if __name__ == "__main__":
    unittest.main()
