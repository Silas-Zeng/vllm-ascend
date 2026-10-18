import unittest

from analyze_context_profiles import numeric, summarize_kernels, summarize_scopes, target_graph_attribution


class ContextProfileTests(unittest.TestCase):
    def test_graph_kernels_are_recovered_when_scope_device_duration_is_zero(self):
        scopes = {
            "study/target_graph/verify": {"calls": 2, "device_total_us": 0},
            "study/target_eager/verify": {"calls": 1, "device_total_us": 3000},
        }
        kernels = {
            "fia_by_model_id": {"0": {"calls": 72, "duration_us": 4000}},
            "model_ids": {"0": {"duration_us": 20000}},
        }
        phases = {"prefill": 0, "mixed": 0, "decode": 0, "verify": 3}
        result = target_graph_attribution(scopes, kernels, phases)
        self.assertAlmostEqual(result["pure_forward_device_ms_per_call"], 23 / 3)
        self.assertEqual(result["graph_device_ms_per_replay"], 10)
        kernels["fia_by_model_id"]["0"]["calls"] = 71
        with self.assertRaisesRegex(ValueError, "36 layers"):
            target_graph_attribution(scopes, kernels, phases)

    def test_missing_utilization_is_not_zero(self):
        self.assertIsNone(numeric("N/A\t"))
        self.assertIsNone(numeric("NaN"))
        self.assertIsNone(numeric("inf"))
        self.assertEqual(numeric(" 12.5\t"), 12.5)

    def test_nested_host_scopes_do_not_become_device_elapsed_time(self):
        rows = [
            {
                "Name": "study/draft_total/decode",
                "Host Total Duration(us)": "10000",
                "Host Self Duration(us)": "1000",
                "Device Total Duration(us)": "2500",
                "Device Total Duration With AICore(us)": "2000",
            },
            {
                "Name": "study/draft_backbone/decode",
                "Host Total Duration(us)": "4000",
                "Host Self Duration(us)": "500",
                "Device Total Duration(us)": "1500",
                "Device Total Duration With AICore(us)": "1000",
            },
        ]
        result = summarize_scopes(rows)
        self.assertEqual(result["study/draft_total/decode"]["device_total_ms_per_call"], 2.5)
        self.assertEqual(result["study/draft_total/decode"]["host_total_ms_per_call"], 10)
        self.assertEqual(result["study/draft_backbone/decode"]["device_total_ms_per_call"], 1.5)

    def test_missing_metric_remains_missing(self):
        result = summarize_scopes([{"Name": "study/target_forward", "Host Total Duration(us)": "N/A"}])
        self.assertIsNone(result["study/target_forward"]["host_total_ms_per_call"])
        self.assertIsNone(result["study/target_forward"]["device_total_ms_per_call"])

    def test_graph_and_eager_attention_are_counted_separately(self):
        rows = [
            {"Type": "FusedInferAttentionScore", "Model ID": "0", "Duration(us)": "10"},
            {"Type": "FusedInferAttentionScore", "Model ID": "4294967295", "Duration(us)": "3"},
            {"Type": "MatMul", "Model ID": "4294967295", "Duration(us)": "7"},
        ]
        result = summarize_kernels(rows)
        self.assertEqual(result["fia_by_model_id"]["0"]["duration_us"], 10)
        self.assertEqual(result["fia_by_model_id"]["4294967295"]["duration_us"], 3)
        self.assertEqual(result["types"]["FusedInferAttentionScore"]["cumulative_duration_share"], 0.65)
        self.assertEqual(result["available_duration_weighted_utilization"], {})


if __name__ == "__main__":
    unittest.main()
