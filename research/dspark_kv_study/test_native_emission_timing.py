import unittest
from types import SimpleNamespace

from native_emission_timing import generate_native_with_timing


class NativeEmissionTimingTests(unittest.TestCase):
    def test_native_submission_and_return_objects_are_preserved(self):
        class Engine:
            def __init__(self):
                self.output_processor = SimpleNamespace(process_outputs=lambda outputs: outputs)

            def add_request(self, request_id, prompt, params):
                self.params = params
                return "internal"

        engine = Engine()
        params = SimpleNamespace(output_kind="final_only")
        result = []

        class LLM:
            llm_engine = engine

            def generate(self, prompts, sampling, use_tqdm):
                engine.add_request("native-id", prompts[0], sampling)
                first = [SimpleNamespace(request_id="internal", new_token_ids=[1, 2], finished=False)]
                last = [SimpleNamespace(request_id="internal", new_token_ids=[3, 4, 5], finished=True)]
                self_outer.assertIs(engine.output_processor.process_outputs(first), first)
                self_outer.assertIs(engine.output_processor.process_outputs(last), last)
                result.append(
                    SimpleNamespace(request_id="internal", outputs=[SimpleNamespace(token_ids=[1, 2, 3, 4, 5])])
                )
                return result

        self_outer = self
        original_add, original_process = engine.add_request, engine.output_processor.process_outputs
        moments = iter((0, 0.001, 0.010, 0.025, 0.026))
        responses, timings, seconds = generate_native_with_timing(LLM(), [{}], params, lambda: next(moments))
        self.assertIs(responses, result)
        self.assertIs(engine.params, params)
        self.assertEqual(engine.add_request, original_add)
        self.assertIs(engine.output_processor.process_outputs, original_process)
        self.assertEqual(params.output_kind, "final_only")
        self.assertAlmostEqual(timings[0]["ttft_ms"], 9)
        self.assertAlmostEqual(timings[0]["tpot_ms"], 5)
        self.assertEqual(timings[0]["first_output_tokens"], 2)
        self.assertAlmostEqual(seconds, 0.026)

    def test_observers_are_restored_on_failure(self):
        engine = SimpleNamespace(
            add_request=lambda: None, output_processor=SimpleNamespace(process_outputs=lambda: None)
        )
        original_add, original_process = engine.add_request, engine.output_processor.process_outputs

        class LLM:
            llm_engine = engine

            def generate(self, *args, **kwargs):
                raise RuntimeError("native failure")

        with self.assertRaisesRegex(RuntimeError, "native failure"):
            generate_native_with_timing(LLM(), [{}], object())
        self.assertIs(engine.add_request, original_add)
        self.assertIs(engine.output_processor.process_outputs, original_process)


if __name__ == "__main__":
    unittest.main()
