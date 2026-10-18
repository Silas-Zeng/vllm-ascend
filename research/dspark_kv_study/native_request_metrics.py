"""Read existing native metrics only when requests finish; keep generation unchanged."""

import dataclasses
import math
import time


def generate_with_native_metrics(llm, prompts, sampling, clock=time.perf_counter):
    processor = llm.llm_engine.output_processor
    original_finish = processor._update_stats_from_finished
    records = {}

    def finished(*args, **kwargs):
        request = args[0] if args else kwargs["req_state"]
        iteration = args[2] if len(args) > 2 else kwargs["iteration_stats"]
        if iteration is None or request.stats is None:
            raise ValueError("native per-request statistics are disabled")
        before = len(iteration.finished_requests)
        result = original_finish(*args, **kwargs)
        if len(iteration.finished_requests) != before + 1:
            raise ValueError("native finish callback did not append one metric record")
        native = dataclasses.asdict(iteration.finished_requests[-1])
        state = dataclasses.asdict(request.stats)
        request_id = request.external_req_id
        if native["request_id"] != request_id or request_id in records:
            raise ValueError("native metric request identity mismatch")
        records[request_id] = {
            "source": "existing native RequestStateStats and FinishedRequestStats",
            "ttft_ms": state["first_token_latency"] * 1000,
            "tpot_ms": native["mean_time_per_output_token"] * 1000,
            "latency_ms": native["e2e_latency"] * 1000,
            "queued_ms": native["queued_time"] * 1000,
            "prefill_ms": native["prefill_time"] * 1000,
            "decode_ms": native["decode_time"] * 1000,
            "inference_ms": native["inference_time"] * 1000,
            "native_request_state_stats": state,
            "native_finished_request_stats": native,
        }
        return result

    processor._update_stats_from_finished = finished
    started = clock()
    try:
        responses = llm.generate(prompts, sampling, use_tqdm=False)
    finally:
        processor._update_stats_from_finished = original_finish
    seconds = clock() - started
    timings = []
    for response in responses:
        timing = records[response.request_id]
        count = len(response.outputs[0].token_ids)
        native = timing["native_finished_request_stats"]
        state = timing["native_request_state_stats"]
        if native["num_generation_tokens"] != count or state["num_generation_tokens"] != count:
            raise ValueError("native metric token count differs from final generated output")
        if count > 1 and not math.isclose(
            native["mean_time_per_output_token"], (state["last_token_ts"] - state["first_token_ts"]) / (count - 1)
        ):
            raise ValueError("native TPOT does not match core monotonic timestamps")
        if not all(math.isfinite(timing[key]) and timing[key] > 0 for key in ("ttft_ms", "tpot_ms", "latency_ms")):
            raise ValueError("native request lacks valid timing statistics")
        timings.append(timing)
    if len(responses) != len(prompts) or len(records) != len(prompts):
        raise ValueError("native request metric count mismatch")
    return responses, timings, seconds
