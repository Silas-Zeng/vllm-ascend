import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent / "deployment"))
from collect_context_evidence import compact_trace, digest, main, pack
from make_profile_analysis_job import main as make_profile_analysis


class ContextArchiveTests(unittest.TestCase):
    def test_numeric_epoch_timestamp_is_not_rounded_through_float(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "trace.json"
            source.write_text(
                '{"traceEvents":[{"name":"kernel","ph":"X","ts":1792290000000010.124,'
                '"dur":2.86,"args":{"Task Type":"AI_CORE","Model Id":0}}]}'
            )
            compact_trace(source, root / "compact.gz")
            event = json.loads(gzip.decompress((root / "compact.gz").read_bytes()))["traceEvents"][0]
            self.assertEqual(event["ts"], "1792290000000010.124")
            self.assertEqual(event["dur"], "2.86")

    def test_cpu_sdk_parsing_waits_until_npu_profiles_finish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "state").mkdir()
            (root / "state/profile_result.json").write_text(json.dumps({"status": "running"}))
            with self.assertRaisesRegex(ValueError, "not all terminal"):
                make_profile_analysis(SimpleNamespace(root=root, profile_name="profile", name="parse"))
            self.assertFalse((root / "state/parse_spec.json").exists())

    def test_raw_bytes_survive_deterministic_compression(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "raw.json"
            source.write_bytes(b'{"true_raw_measurement":1}\n')
            item = pack(source, folder / "one.gz", True)
            pack(source, folder / "two.gz", True)
            self.assertEqual(gzip.decompress((folder / "one.gz").read_bytes()), source.read_bytes())
            self.assertEqual(item["raw_sha256"], digest(source))
            self.assertEqual(digest(folder / "one.gz"), digest(folder / "two.gz"))

    def test_compact_trace_preserves_real_timestamps_and_native_task_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "trace.json"
            cpu = {"name": "study/target_verify", "ph": "X", "ts": "12345.125", "dur": 7.25}
            hardware = {
                "name": "kernel",
                "ph": "X",
                "ts": "12346.125",
                "dur": 2.5,
                "args": {"Task Type": "AI_CORE", "Model Id": 0},
            }
            ignored = {"name": "aten::add", "ph": "X", "ts": "12345", "dur": 1}
            source.write_text(json.dumps({"traceEvents": [cpu, hardware, ignored]}))
            compact_trace(source, folder / "compact.gz")
            result = json.loads(gzip.decompress((folder / "compact.gz").read_bytes()))
            self.assertEqual(result["traceEvents"], [{**cpu, "dur": "7.25"}, {**hardware, "dur": "2.5"}])
            self.assertEqual(result["study_provenance"]["source_sha256"], digest(source))

    def test_active_jobs_cannot_be_sealed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "state").mkdir()
            (root / "state/context_formal_v1_20261018_result.json").write_text(json.dumps({"status": "running"}))
            with self.assertRaisesRegex(ValueError, "incomplete/failed"):
                main(
                    SimpleNamespace(
                        root=root,
                        timing_name="context_formal_v1_20261018",
                        timing_steps=30,
                        profile_name="unused",
                        output=root / "archive",
                    )
                )
            self.assertFalse((root / "archive").exists())


if __name__ == "__main__":
    unittest.main()
