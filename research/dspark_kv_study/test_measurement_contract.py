import unittest

from benchmark_attention import trace_events
from run_vllm_baseline import model_options, request_tpot_ms


class MeasurementContractTests(unittest.TestCase):
    def test_torchnpu_and_chrome_trace_export_formats(self):
        events = [{"ph": "X", "name": "kv_study/anchors_cached", "dur": 10}]
        self.assertEqual(trace_events(events), events)
        self.assertEqual(trace_events({"traceEvents": events}), events)
        with self.assertRaises(ValueError):
            trace_events({"traceEvents": "invalid"})

    def test_window_changes_only_draft_additional_config(self):
        full = model_options("full", "target", "draft", 32768, 4, 42)
        window = model_options("window", "target", "draft", 32768, 4, 42)
        self.assertEqual(window.pop("additional_config"), {"draft_window_size": 2176})
        self.assertEqual(full, window)

    def test_ar_and_unimplemented_modes(self):
        options = model_options("ar", "target", None, 32768, 4, 42)
        self.assertNotIn("speculative_config", options)
        with self.assertRaises(ValueError):
            model_options("anchors", "target", "draft", 32768, 4, 42)
        with self.assertRaises(ValueError):
            model_options("full", "target", None, 32768, 4, 42)

    def test_sparse_and_fusion_controls_are_explicit(self):
        config = {"local_tokens": 2048, "anchor_tokens": 128, "policy": "bm25", "cache_anchors": True}
        options = model_options("anchors", "target", "draft", 32768, 4, 42, config, True)
        self.assertEqual(
            options["additional_config"],
            {
                "research_history_kv": config,
                "ascend_compilation_config": {"fuse_qknorm_rope": False},
            },
        )
        self.assertEqual(options["speculative_config"]["num_speculative_tokens"], 7)

    def test_tpot_uses_decode_interval_and_output_gaps(self):
        self.assertEqual(request_tpot_ms(2, 3, 101), 10)
        self.assertIsNone(request_tpot_ms(None, None, 512))
        self.assertIsNone(request_tpot_ms(0, 1, 1))
        self.assertIsNone(request_tpot_ms(float("nan"), 1, 10))
        with self.assertRaises(ValueError):
            request_tpot_ms(2, 1, 10)


if __name__ == "__main__":
    unittest.main()
