"""Reject incomplete, mislabeled, or inconsistent measurements using real data."""

import copy
import json
import unittest
from pathlib import Path

from analyze_context_matrix import common_prefix, distribution, validate_report


class ContextEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = Path(__file__).parent / "evidence/context_20261018/legacy_formal"
        cls.ar = json.loads((folder / "ar_c4096_b1.json").read_text())
        cls.full = json.loads((folder / "full_c4096_b1.json").read_text())
        cls.native = json.loads((folder.parent / "formal_native/ar_c4096_b1.json").read_text())
        cls.native_variation = json.loads((folder.parent / "formal_native/full_c4096_b4.json").read_text())
        cls.profile = json.loads((folder.parent / "profile_runs/ar_c4096_b1.json").read_text())

    def test_real_profile_cannot_be_mixed_into_formal_performance(self):
        validate_report(self.profile, "ar", 4096, 1, "native_stats", profiled=True)
        with self.assertRaisesRegex(ValueError, "profile/nonprofile"):
            validate_report(self.profile, "ar", 4096, 1, "native_stats")

    def test_real_native_variation_is_preserved_without_false_pass(self):
        report = copy.deepcopy(self.native_variation)
        validate_report(report, "full", 4096, 4, expected_timing_method="native_stats")
        self.assertEqual(sum(run["matches_generate_control"] for run in report["runs"]), 8)
        report["native_control_comparison_policy"] = "strict"
        with self.assertRaisesRegex(ValueError, "instability under strict"):
            validate_report(report, "full", 4096, 4)
        report["native_control_comparison_policy"] = "record_variation"
        next(run for run in report["runs"] if not run["matches_generate_control"])["matches_generate_control"] = True
        with self.assertRaisesRegex(ValueError, "flag contradicts tokens"):
            validate_report(report, "full", 4096, 4)

    def test_real_native_statistics_use_separate_clock_domains(self):
        validate_report(self.native, "ar", 4096, 1, expected_timing_method="native_stats")
        report = copy.deepcopy(self.native)
        timing = report["runs"][0]["observed_request_timing"][0]
        state = timing["native_request_state_stats"]
        timing["ttft_ms"] = (state["first_token_ts"] - state["arrival_time"]) * 1000
        with self.assertRaisesRegex(ValueError, "frontend TTFT"):
            validate_report(report, "ar", 4096, 1)

    def test_native_metric_token_count_must_match_final_output(self):
        report = copy.deepcopy(self.native)
        report["runs"][0]["observed_request_timing"][0]["native_request_state_stats"]["num_generation_tokens"] = 511
        with self.assertRaisesRegex(ValueError, "native token count"):
            validate_report(report, "ar", 4096, 1)

    def test_completed_real_reports(self):
        validate_report(self.ar, "ar", 4096, 1)
        validate_report(self.full, "full", 4096, 1)
        self.assertLess(
            common_prefix(self.ar["runs"][0]["output_token_ids"][0], self.full["runs"][0]["output_token_ids"][0]), 512
        )

    def test_incomplete_matrix_member_is_rejected(self):
        report = copy.deepcopy(self.full)
        report["runs"].pop()
        with self.assertRaisesRegex(ValueError, "incomplete repetitions"):
            validate_report(report, "full", 4096, 1)

    def test_nominal_budget_is_not_actual_budget(self):
        report = copy.deepcopy(self.full)
        report["resolved_scheduler_config"]["max_num_scheduled_tokens"] = 4090
        with self.assertRaisesRegex(ValueError, "actual scheduler"):
            validate_report(report, "full", 4096, 1)

    def test_wrong_counters_are_rejected(self):
        report = copy.deepcopy(self.full)
        report["runs"][0]["speculative_counter_deltas"]["vllm:spec_decode_num_draft_tokens"] += 1
        with self.assertRaisesRegex(ValueError, "speculative counters"):
            validate_report(report, "full", 4096, 1)

    def test_duplicate_emissions_are_rejected(self):
        report = copy.deepcopy(self.full)
        report["runs"][0]["observed_request_timing"][0]["events"][1]["output_tokens"] = 1
        with self.assertRaisesRegex(ValueError, "nonmonotonic"):
            validate_report(report, "full", 4096, 1)

    def test_quality_instability_is_rejected(self):
        report = copy.deepcopy(self.full)
        report["runs"][0]["output_token_ids"][0][0] += 1
        with self.assertRaisesRegex(ValueError, "within-method instability"):
            validate_report(report, "full", 4096, 1)

    def test_invalid_samples_do_not_become_plots(self):
        with self.assertRaises(ValueError):
            distribution([1.0, float("nan")])
        self.assertEqual(distribution([1.0, 2.0, 3.0, 4.0])["median"], 2.5)


if __name__ == "__main__":
    unittest.main()
