"""Observe accepted EngineCore token arrivals during unchanged FINAL_ONLY generation."""

import time


def generate_native_with_timing(llm, prompts, sampling, clock=time.perf_counter):
    engine = llm.llm_engine
    original_add = engine.add_request
    processor = engine.output_processor
    original_process = processor.process_outputs
    records = {}
    aliases = {}
    start = clock()

    def add_request(*args, **kwargs):
        request_id = args[0] if args else kwargs["request_id"]
        record = {
            "arrival": clock(),
            "first": None,
            "last_token": None,
            "finish": None,
            "tokens": [],
            "events": [],
            "num_nans_in_logits": 0,
        }
        records[request_id] = record
        returned = original_add(*args, **kwargs)
        aliases[request_id] = request_id
        if returned is not None:
            aliases[returned] = request_id
        return returned

    def process_outputs(*args, **kwargs):
        outputs = args[0] if args else kwargs["engine_core_outputs"]
        now = clock()
        for output in outputs:
            if output.request_id not in aliases:
                raise ValueError("native core observer received an unknown request")
            record = records[aliases[output.request_id]]
            record["tokens"].extend(output.new_token_ids)
            count = len(record["tokens"])
            if output.new_token_ids:
                if record["first"] is None:
                    record["first"] = now
                    record["first_count"] = count
                record["last_token"] = now
            record["num_nans_in_logits"] = max(record["num_nans_in_logits"], getattr(output, "num_nans_in_logits", 0))
            record["events"].append(
                {"time_ms": (now - start) * 1000, "output_tokens": count, "finished": output.finished}
            )
            if output.finished:
                record["finish"] = now
        return original_process(*args, **kwargs)

    engine.add_request, processor.process_outputs = add_request, process_outputs
    try:
        responses = llm.generate(prompts, sampling, use_tqdm=False)
    finally:
        engine.add_request, processor.process_outputs = original_add, original_process
    seconds = clock() - start
    timings = []
    for response in responses:
        record = records[aliases[response.request_id]]
        if record["first"] is None or record["finish"] is None:
            raise ValueError("native request lacks first-token or completion event")
        count = len(response.outputs[0].token_ids)
        if record["tokens"] != list(response.outputs[0].token_ids):
            raise ValueError("observed accepted core tokens differ from the native final output")
        gaps = count - record["first_count"]
        timings.append(
            {
                "ttft_ms": (record["first"] - record["arrival"]) * 1000,
                "latency_ms": (record["finish"] - record["arrival"]) * 1000,
                "tpot_ms": (record["last_token"] - record["first"]) * 1000 / gaps if gaps > 0 else None,
                "first_output_tokens": record["first_count"],
                "num_nans_in_logits": record["num_nans_in_logits"],
                "events": record["events"],
            }
        )
    if len(responses) != len(prompts) or len(records) != len(prompts):
        raise ValueError("native measurement request count mismatch")
    return responses, timings, seconds
