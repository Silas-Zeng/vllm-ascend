"""Observe real cumulative engine outputs for offline request timing.

TPOT uses the first and last observed output events divided by output_tokens-1.
It is not average inter-event latency when speculation emits several tokens.
"""

import copy
import time


def generate_with_timing(engine, prompts, sampling, output_kind, request_prefix, clock=time.perf_counter):
    start = clock()
    aliases = {}
    records = []
    responses = [None] * len(prompts)
    for index, prompt in enumerate(prompts):
        request_id = f"{request_prefix}-{index}"
        params = copy.deepcopy(sampling[index] if isinstance(sampling, list) else sampling)
        params.output_kind = output_kind
        arrived = clock()
        returned_id = engine.add_request(request_id, prompt, params)
        aliases[request_id] = aliases[returned_id] = index
        records.append({"arrival_seconds": arrived, "first_seconds": None, "finish_seconds": None, "events": []})
    while engine.has_unfinished_requests():
        outputs = engine.step()
        now = clock()
        for output in outputs:
            if output.request_id not in aliases:
                raise ValueError("engine output does not belong to this measurement batch")
            index = aliases[output.request_id]
            if len(output.outputs) != 1:
                raise ValueError("this measurement contract supports one sequence per request")
            count = len(output.outputs[0].token_ids)
            record = records[index]
            if count and record["first_seconds"] is None:
                record["first_seconds"] = now
            record["events"].append(
                {"time_ms": (now - start) * 1000, "output_tokens": count, "finished": output.finished}
            )
            if output.finished:
                record["finish_seconds"] = now
                responses[index] = output
    seconds = clock() - start
    timings = []
    for response, record in zip(responses, records, strict=True):
        if response is None or record["first_seconds"] is None or record["finish_seconds"] is None:
            raise ValueError("request finished without a complete observed output sequence")
        count = len(response.outputs[0].token_ids)
        timings.append(
            {
                "ttft_ms": (record["first_seconds"] - record["arrival_seconds"]) * 1000,
                "latency_ms": (record["finish_seconds"] - record["arrival_seconds"]) * 1000,
                "tpot_ms": (record["finish_seconds"] - record["first_seconds"]) * 1000 / (count - 1)
                if count > 1
                else None,
                "first_output_tokens": next(
                    event["output_tokens"] for event in record["events"] if event["output_tokens"]
                ),
                "events": record["events"],
            }
        )
    return responses, timings, seconds
