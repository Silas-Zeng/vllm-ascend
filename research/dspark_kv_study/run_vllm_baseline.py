"""AR / Full-KV / window baselines for a separately provisioned DSpark stack.

Not executed on the recorded CANN 9.0 server: vLLM and weights are absent.
Historical anchors are intentionally not exposed as a serving mode until the
backend integration and model-output validation have actually been implemented.
"""

import argparse
import hashlib
import json
import math
import time
from pathlib import Path


def model_options(mode, target, draft, max_model_len, concurrency, seed):
    if mode not in ("ar", "full", "window"):
        raise ValueError("unsupported or unintegrated mode")
    options = {
        "model": str(target),
        "dtype": "bfloat16",
        "tensor_parallel_size": 1,
        "distributed_executor_backend": "mp",
        "max_model_len": max_model_len,
        "max_num_seqs": concurrency,
        "enable_prefix_caching": False,
        "gpu_memory_utilization": 0.8,
        "disable_log_stats": False,
        "seed": seed,
    }
    if mode != "ar":
        if draft is None:
            raise ValueError("DSpark modes require a matching draft checkpoint")
        options["speculative_config"] = {
            "method": "dspark",
            "model": str(draft),
            "num_speculative_tokens": 7,
            "enforce_eager": True,
        }
    if mode == "window":
        options["additional_config"] = {"draft_window_size": 2176}
    return options


def request_tpot_ms(first_token_time, finished_time, output_count):
    # Offline aggregate throughput is not a substitute for request-level TPOT.
    if output_count <= 1 or first_token_time is None or finished_time is None:
        return None
    if not all(math.isfinite(value) for value in (first_token_time, finished_time)):
        return None
    if finished_time < first_token_time:
        raise ValueError("request timestamps are out of order")
    return (finished_time - first_token_time) * 1000 / (output_count - 1)


def spec_counters(llm):
    reader = getattr(llm, "get_metrics", None)
    if reader is None:
        return {}
    counters = {}
    for metric in reader():
        name, value = getattr(metric, "name", ""), getattr(metric, "value", None)
        if "spec_decode" in name and isinstance(value, (int, float)):
            counters[name] = value
    return counters


def main(args):
    if not args.target.is_dir() or (args.mode != "ar" and (args.draft is None or not args.draft.is_dir())):
        raise ValueError("provide existing local checkpoint directories; this runner does not download models")
    data = json.loads(args.prompts.read_text(encoding="utf-8"))
    ids = [item["request_id"] for item in data]
    if len(data) != args.concurrency or len(set(ids)) != len(ids):
        raise ValueError("provide exactly concurrency requests with distinct IDs")
    token_lists = [item["prompt_token_ids"] for item in data]
    if not all(tokens and all(isinstance(token, int) and token >= 0 for token in tokens) for tokens in token_lists):
        raise ValueError("each request needs valid, already tokenized prompt IDs")
    longest = max(map(len, token_lists))
    prompt_hash = hashlib.sha256(args.prompts.read_bytes()).hexdigest()
    reference = json.loads(args.reference.read_text(encoding="utf-8")) if args.reference else None
    if reference is not None and reference.get("mode") != "ar":
        raise ValueError("the correctness reference must be an autoregressive baseline")
    if reference is not None and (
        reference["prompt_sha256"] != prompt_hash or reference["output_tokens"] != args.output_tokens
    ):
        raise ValueError("AR reference and candidate must use identical prompts and output length")

    from vllm import LLM, SamplingParams
    from vllm.config import CompilationConfig

    options = model_options(
        args.mode, args.target, args.draft, longest + args.output_tokens + 8, args.concurrency, args.seed
    )
    options["compilation_config"] = CompilationConfig(cudagraph_mode="FULL_DECODE_ONLY")
    llm = LLM(**options)
    sampling = SamplingParams(temperature=0, max_tokens=args.output_tokens, ignore_eos=True, seed=args.seed)
    prompts = [{"prompt_token_ids": tokens} for tokens in token_lists]
    for _ in range(args.warmup):
        llm.generate(prompts, sampling, use_tqdm=False)
    report = {
        "scope": "offline_vllm_generation_including_prefill",
        "mode": args.mode,
        "prompt_sha256": prompt_hash,
        "prompt_lengths": list(map(len, token_lists)),
        "output_tokens": args.output_tokens,
        "seed": args.seed,
        "concurrency": args.concurrency,
        "runs": [],
    }
    for iteration in range(args.repeats):
        before = spec_counters(llm)
        start = time.perf_counter()
        responses = llm.generate(prompts, sampling, use_tqdm=False)
        seconds = time.perf_counter() - start
        after = spec_counters(llm)
        output_ids = [list(response.outputs[0].token_ids) for response in responses]
        if len(output_ids) != len(data) or any(len(tokens) != args.output_tokens for tokens in output_ids):
            raise ValueError("generation did not produce the requested fixed workload")
        matched = output_ids == reference["runs"][0]["output_token_ids"] if reference is not None else None
        if matched is False:
            raise AssertionError("greedy output tokens differ from AR reference")
        tpots = []
        for response, tokens in zip(responses, output_ids, strict=True):
            metrics = getattr(response, "metrics", None)
            tpots.append(
                request_tpot_ms(
                    getattr(metrics, "first_token_time", None), getattr(metrics, "finished_time", None), len(tokens)
                )
            )
        report["runs"].append(
            {
                "iteration": iteration,
                "wall_seconds": seconds,
                "output_tokens_per_second_including_prefill": sum(map(len, output_ids)) / seconds,
                "request_tpot_ms": tpots,
                "output_token_ids": output_ids,
                "matches_ar_reference": matched,
                "speculative_counter_deltas": {name: value - before.get(name, 0) for name, value in after.items()},
            }
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["ar", "full", "window"], required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--draft", type=Path)
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output-tokens", type=int, default=512)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parsed = parser.parse_args()
    if min(parsed.concurrency, parsed.output_tokens, parsed.repeats, parsed.warmup) < 1:
        parser.error("concurrency, output tokens, repeats and warmup must be positive")
    main(parsed)
