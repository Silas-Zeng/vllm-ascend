import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "deployment"))
from make_context_job import build_steps
from queue_context_profiles import ready


class ContextJobTest(unittest.TestCase):
    def test_recorded_variation_changes_only_post_generation_guard(self):
        paths = [Path(name) for name in ("/study", "/stage", "/window", "/output")]
        strict = build_steps(*paths, "timing", None, timing_method="native_stats")
        recorded = build_steps(*paths, "timing", None, timing_method="native_stats", record_control_mismatch=True)
        self.assertEqual(len(strict), len(recorded))
        for original, observation in zip(strict, recorded):
            without_flag = [value for value in observation["argv"] if value != "--record-control-mismatch"]
            self.assertEqual(original["argv"], without_flag)
            if observation["name"].endswith("_measure"):
                self.assertIn("--record-control-mismatch", observation["argv"])

    def test_native_metric_pipeline_keeps_formal_control_strict(self):
        paths = [Path(name) for name in ("/study", "/stage", "/window", "/output")]
        timing = build_steps(*paths, "timing", None, timing_method="native_stats")
        profiles = build_steps(*paths, "profile", Path("/timing"), timing_method="native_stats")
        for step in timing:
            if step["name"].endswith("_measure"):
                self.assertEqual(step["argv"][step["argv"].index("--timing-method") + 1], "native_stats")
                self.assertNotIn("--record-control-mismatch", step["argv"])
        for step in profiles:
            self.assertIn("--record-control-mismatch", step["argv"])

    def test_profiles_require_terminal_success_of_every_timing_and_audit(self):
        steps = build_steps(Path("/study"), Path("/stage"), Path("/window"), Path("/output"), "timing", None)
        results = [{"name": step["name"], "exit_code": 0} for step in steps]
        self.assertTrue(ready({"status": "complete", "steps": results}))
        self.assertFalse(ready({"status": "running", "steps": results}))
        self.assertFalse(ready({"status": "complete", "steps": results[:-1]}))
        results[-1]["exit_code"] = 1
        self.assertFalse(ready({"status": "complete", "steps": results}))
        self.assertTrue(ready({"status": "complete", "steps": [{"name": "retry_measure", "exit_code": 0}]}, 1, 0))

    def test_complete_matrix_and_effective_budget(self):
        root, stage, window, output = map(Path, ["/study", "/stage", "/window", "/output"])
        steps = build_steps(root, stage, window, output, "timing", None)
        measurements = [step for step in steps if step["name"].endswith("_measure")]
        self.assertEqual(len(measurements), 18)
        self.assertEqual(len(steps), 30)
        for step in measurements:
            argv = step["argv"]
            value = lambda flag, argv=argv: argv[argv.index(flag) + 1]
            batch = int(value("--concurrency"))
            mode = value("--mode")
            self.assertEqual(value("--max-num-scheduled-tokens"), "4096")
            self.assertEqual(int(value("--max-num-batched-tokens")), 4096 + (6 * batch if mode != "ar" else 0))
            self.assertEqual(value("--max-model-len"), "32768")
            self.assertEqual(value("--output-tokens"), "512")
            self.assertEqual(value("--repeats"), "10")
            self.assertEqual(value("--warmup"), "10")

    def test_profile_is_separate_and_references_formal_ar(self):
        steps = build_steps(
            Path("/study"), Path("/stage"), Path("/window"), Path("/profiles"), "profile", Path("/timing")
        )
        self.assertEqual(len(steps), 18)
        for step in steps:
            argv = step["argv"]
            self.assertIn("--profile-dir", argv)
            self.assertNotIn("--audit-after", argv)
            self.assertTrue(argv[argv.index("--reference") + 1].startswith(str(Path("/timing"))))
            self.assertEqual(argv[argv.index("--repeats") + 1], "1")


if __name__ == "__main__":
    unittest.main()
