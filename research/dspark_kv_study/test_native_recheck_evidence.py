"""Adversarial checks for diagnostic evidence integrity and output comparison."""

import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from plot_native_recheck import read_json, report_row, verify_archive


class DiagnosticEvidenceTest(unittest.TestCase):
    def test_archive_rejects_modified_expanded_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = b"original device log\n"
            compressed = gzip.compress(raw, mtime=0)
            (root / "device.log.gz").write_bytes(compressed)
            record = {
                "file": "device.log.gz",
                "bytes": len(compressed),
                "sha256": hashlib.sha256(compressed).hexdigest(),
                "uncompressed_bytes": len(raw),
                "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
            }
            (root / "archive_manifest.json").write_text(json.dumps([record]))
            (root / "local_path_map.json").write_text(json.dumps({"device.log.gz": "device.log.gz"}))
            self.assertEqual(verify_archive(root), 1)
            record["uncompressed_sha256"] = "0" * 64
            (root / "archive_manifest.json").write_text(json.dumps([record]))
            with self.assertRaisesRegex(ValueError, "expanded checksum mismatch"):
                verify_archive(root)

    def test_archive_rejects_path_outside_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "archive_manifest.json").write_text(json.dumps([{"file": "a.json"}]))
            (root / "local_path_map.json").write_text(json.dumps({"a.json": "../outside.json"}))
            with self.assertRaisesRegex(ValueError, "leaves evidence directory"):
                verify_archive(root)

    def test_real_report_and_token_mutation(self):
        path = Path(__file__).parent / "evidence/server_b2/bounded_diagnosis_20261018/ar_queue1.json"
        report = read_json(path)
        row, reference = report_row(path.name, report, None)
        self.assertEqual(row["measured_runs"], 2)
        corrupted = copy.deepcopy(report)
        corrupted["runs"][1]["output_token_ids"][0][-1] += 1
        with self.assertRaisesRegex(ValueError, "greedy output differs"):
            report_row(path.name, corrupted, reference)

    def test_different_context_cannot_enter_diagnostic_plot(self):
        path = Path(__file__).parent / "evidence/server_b2/bounded_diagnosis_20261018/ar_queue1.json"
        report = read_json(path)
        report["prompt_lengths"] = [4096]
        with self.assertRaisesRegex(ValueError, "configuration mismatch"):
            report_row(path.name, report, None)

    def test_nonfinite_duration_cannot_be_plotted_as_valid(self):
        path = Path(__file__).parent / "evidence/server_b2/bounded_diagnosis_20261018/ar_queue1.json"
        report = read_json(path)
        report["runs"][0]["wall_seconds"] = float("nan")
        with self.assertRaisesRegex(ValueError, "invalid duration"):
            report_row(path.name, report, None)


if __name__ == "__main__":
    unittest.main()
