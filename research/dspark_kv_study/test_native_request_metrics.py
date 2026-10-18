import dataclasses
import unittest
from types import SimpleNamespace

from native_request_metrics import generate_with_native_metrics


@dataclasses.dataclass
class RequestStats:
    arrival_time: float = 1800000000.0
    first_token_latency: float = 0.3
    first_token_ts: float = 1000.0
    last_token_ts: float = 1002.0
    num_generation_tokens: int = 3


@dataclasses.dataclass
class FinishedStats:
    request_id: str = "native-id"
    num_generation_tokens: int = 3
    mean_time_per_output_token: float = 1.0
    e2e_latency: float = 2.4
    queued_time: float = 0.01
    prefill_time: float = 0.2
    decode_time: float = 2.0
    inference_time: float = 2.2


class NativeRequestMetricsTests(unittest.TestCase):
    def test_native_clocks_are_not_subtracted_across_domains(self):
        class Processor:
            def _update_stats_from_finished(self, request, reason, iteration):
                iteration.finished_requests.append(FinishedStats())
                return "original-result"

        processor = Processor()
        original = processor._update_stats_from_finished
        response = [SimpleNamespace(request_id="native-id", outputs=[SimpleNamespace(token_ids=[1, 2, 3])])]
        params = SimpleNamespace(output_kind="final_only")
        test = self

        class LLM:
            llm_engine = SimpleNamespace(output_processor=processor)

            def generate(self, prompts, sampling, use_tqdm):
                test.assertIs(sampling, params)
                value = processor._update_stats_from_finished(
                    SimpleNamespace(external_req_id="native-id", stats=RequestStats()),
                    "length",
                    SimpleNamespace(finished_requests=[]),
                )
                test.assertEqual(value, "original-result")
                return response

        moments = iter((0, 2.5))
        outputs, timings, seconds = generate_with_native_metrics(LLM(), [{}], params, lambda: next(moments))
        self.assertIs(outputs, response)
        self.assertEqual(processor._update_stats_from_finished, original)
        self.assertEqual(params.output_kind, "final_only")
        self.assertEqual(timings[0]["ttft_ms"], 300)
        self.assertEqual(timings[0]["tpot_ms"], 1000)
        self.assertEqual(timings[0]["latency_ms"], 2400)
        self.assertEqual(seconds, 2.5)
        self.assertNotIn("events", timings[0])

    def test_observer_is_restored_if_generation_fails(self):
        processor = SimpleNamespace(_update_stats_from_finished=lambda *args: None)
        original = processor._update_stats_from_finished

        class LLM:
            llm_engine = SimpleNamespace(output_processor=processor)

            def generate(self, *args, **kwargs):
                raise RuntimeError("native failure")

        with self.assertRaisesRegex(RuntimeError, "native failure"):
            generate_with_native_metrics(LLM(), [{}], object())
        self.assertIs(processor._update_stats_from_finished, original)


if __name__ == "__main__":
    unittest.main()
