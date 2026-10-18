import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from plot_context_profiles import draft_parts, hardware_row, plot_timeline, relative_interval


class ContextProfilePlotTests(unittest.TestCase):
    def test_waits_and_copies_are_not_labeled_graph_compute(self):
        event = {"name": "EVENT_WAIT", "args": {"Task Type": "EVENT_WAIT", "Model Id": 0}}
        self.assertEqual(hardware_row(event, {"0"}), 8)
        event["args"]["Task Type"] = "SDMA_SQE"
        self.assertEqual(hardware_row(event, {"0"}), 7)
        event["args"]["Task Type"] = "AI_VECTOR_CORE"
        self.assertEqual(hardware_row(event, {"0"}), 4)
        event["name"] = "FusedInferAttentionScore"
        event["args"].update({"Task Type": "MIX_AIC", "Model Id": 4294967295})
        self.assertEqual(hardware_row(event, {"0"}), 5)

    def test_real_trace_export_keeps_requested_filename(self):
        import matplotlib

        matplotlib.use("Agg")
        root = Path(__file__).parent
        items = json.loads((root / "results/context_20261018/profile_summary.json").read_text())["configurations"]
        label = "full_c24576_b4"
        item = next(x for x in items if x["label"] == label)
        ids = {label: set(item["target_attribution"]["graph_models_with_target_attention"])}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            plot_timeline(root / "evidence/context_20261018/profiles", folder, [label], "requested_timeline", ids)
            self.assertEqual(
                {path.name for path in folder.iterdir()}, {"requested_timeline.png", "requested_timeline.svg"}
            )

    def test_absolute_timestamp_precision_is_preserved(self):
        origin = Decimal("1792290000000000.123")
        interval = relative_interval("1792290000000010.124", "2.86", origin, origin + 120000)
        self.assertAlmostEqual(interval[0], 0.010001, places=12)
        self.assertAlmostEqual(interval[1], 0.00286, places=12)

    def test_attention_is_not_counted_twice_with_backbone(self):
        durations = {
            "draft_total": 20000,
            "draft_attention": 4000,
            "draft_backbone": 10000,
            "draft_inputs": 1000,
            "draft_metadata": 0,
            "draft_context_cache": 2000,
            "draft_logits": 4000,
            "markov_embed": 0,
            "markov_bias": 1000,
        }
        item = {
            "label": "unit-test",
            "scopes": {
                "study/" + key + "/decode": {"calls": 2, "device_total_us": value} for key, value in durations.items()
            },
        }
        parts = draft_parts(item)
        self.assertEqual(sum(parts.values()), 10)
        self.assertEqual(parts["Backbone outside attention"], 3)
        item["scopes"]["study/draft_attention/decode"]["device_total_us"] = 20000
        with self.assertRaisesRegex(ValueError, "overlapping/missing"):
            draft_parts(item)


if __name__ == "__main__":
    unittest.main()
