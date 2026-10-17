import unittest
from types import SimpleNamespace

from emission_timing import generate_with_timing


class EmissionTimingTests(unittest.TestCase):
    def test_multitoken_events_use_output_gaps_not_event_gaps(self):
        moments = iter((0, 0.001, 0.010, 0.020, 0.021))

        class Engine:
            def __init__(self):
                self.step_number = 0

            def add_request(self, request_id, prompt, params):
                self.request_id = request_id
                self.output_kind = params.output_kind
                return "internal-id"

            def has_unfinished_requests(self):
                return self.step_number < 2

            def step(self):
                self.step_number += 1
                tokens = [1] if self.step_number == 1 else [1, 2, 3]
                return [
                    SimpleNamespace(
                        request_id="internal-id",
                        outputs=[SimpleNamespace(token_ids=tokens)],
                        finished=self.step_number == 2,
                    )
                ]

        engine = Engine()
        response, timing, seconds = generate_with_timing(
            engine, [{}], SimpleNamespace(), "cumulative", "test", lambda: next(moments)
        )
        self.assertEqual(response[0].outputs[0].token_ids, [1, 2, 3])
        self.assertAlmostEqual(timing[0]["ttft_ms"], 9)
        self.assertAlmostEqual(timing[0]["tpot_ms"], 5)
        self.assertAlmostEqual(seconds, 0.021)
        self.assertEqual(engine.output_kind, "cumulative")


if __name__ == "__main__":
    unittest.main()
