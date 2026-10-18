import unittest
from enum import Enum
from types import SimpleNamespace

from study_worker_hooks import target_execution, target_phase


class WorkerObserverTest(unittest.TestCase):
    def test_actual_graph_wrapper_branch_and_lazy_capture(self):
        mode = Enum("Mode", ["NONE", "FULL"])
        context = SimpleNamespace(cudagraph_runtime_mode=mode.FULL, batch_descriptor="batch")
        wrapper = SimpleNamespace(
            runtime_mode=mode.FULL, concrete_aclgraph_entries={"batch": SimpleNamespace(aclgraph=object())}
        )
        self.assertEqual(target_execution(context, wrapper), "graph")
        wrapper.concrete_aclgraph_entries["batch"].aclgraph = None
        self.assertEqual(target_execution(context, wrapper), "capture")
        context.cudagraph_runtime_mode = mode.NONE
        self.assertEqual(target_execution(context, wrapper), "eager")
        self.assertEqual(target_execution(None, wrapper), "unknown")

    def test_mixed_prefill_is_not_counted_as_pure_verification(self):
        self.assertEqual(target_phase(SimpleNamespace(num_prefills=1, num_decodes=3), True), "mixed")
        self.assertEqual(target_phase(SimpleNamespace(num_prefills=1, num_decodes=0), True), "prefill")
        self.assertEqual(target_phase(SimpleNamespace(num_prefills=0, num_decodes=4), True), "verify")
        self.assertEqual(target_phase(SimpleNamespace(num_prefills=0, num_decodes=4), False), "decode")
        self.assertEqual(target_phase(None, True), "unknown")


if __name__ == "__main__":
    unittest.main()
