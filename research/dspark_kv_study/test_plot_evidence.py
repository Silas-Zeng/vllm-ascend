import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from plot_backend_validation import read_controls
from plot_profile import extract_profile
from plot_results import load_evidence


class EvidenceTests(unittest.TestCase):
    def report(self):
        names = ("full_kv", "window_2176", "anchors_online", "anchors_cached")
        return {
            "scope": "synthetic_dense_attention_component_only",
            "is_end_to_end_dspark": False,
            "script_sha256": "test-fixture",
            "policy_sha256": "test-fixture",
            "configuration": {"repeats": 10, "batches": [1], "contexts": [4096]},
            "environment": {},
            "method": {},
            "cases": [
                {
                    "batch": 1,
                    "context_tokens": 4096,
                    "paths": {name: {"samples_ms": [0.1] * 10} for name in names},
                    "correctness": {name: {"passed": True} for name in names},
                }
            ],
        }

    def load_pair(self, first, second):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f"{index}.json" for index in range(2)]
            for path, report in zip(paths, (first, second), strict=True):
                path.write_text(json.dumps(report), encoding="utf-8")
            return load_evidence(paths)

    def test_valid_repeated_component_measurements(self):
        report = self.report()
        self.assertEqual(len(self.load_pair(report, copy.deepcopy(report))), 2)

    def test_refuses_failed_fabricated_or_mismatched_comparisons(self):
        first = self.report()
        for mutation in ("script", "correctness", "samples", "end_to_end", "matrix"):
            second = copy.deepcopy(first)
            if mutation == "script":
                second["script_sha256"] = "changed"
            elif mutation == "correctness":
                second["cases"][0]["correctness"]["full_kv"]["passed"] = False
            elif mutation == "samples":
                second["cases"][0]["paths"]["full_kv"]["samples_ms"][0] = float("nan")
            elif mutation == "end_to_end":
                second["is_end_to_end_dspark"] = True
            else:
                second["cases"] = []
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.load_pair(first, second)

    def test_profile_refuses_cpu_only_trace(self):
        with self.assertRaises(ValueError):
            extract_profile([])

    def test_backend_controls_require_executed_cases_and_matching_sources(self):
        evidence = Path(__file__).parent / "evidence/server_b2"
        self.assertEqual(len(read_controls(evidence)), 8)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in evidence.glob("*_fia.json"):
                shutil.copyfile(path, root / path.name)
            path = root / "full_b3_q7_fia.json"
            original = json.loads(path.read_text(encoding="utf-8"))
            for mutation in ("unexecuted", "source", "missing_error"):
                candidate = copy.deepcopy(original)
                if mutation == "unexecuted":
                    candidate["checks"] = []
                elif mutation == "source":
                    candidate["source_sha256"]["paged_history.py"] = "different-code"
                else:
                    candidate["checks"][0].pop("error")
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    read_controls(root)


if __name__ == "__main__":
    unittest.main()
