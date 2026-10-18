"""AR / Full-KV / window baselines for a pinned, isolated DSpark stack.

Executed in the verified CANN 9.1 image; the host's CANN 9.0 stack is separate.
Historical anchors require the checksum-validated experimental backend overlay.
Its model-output validation remains a separate experimental gate.
"""

import argparse
import hashlib
import json
import math
import time
from importlib import metadata
from pathlib import Path

from emission_timing import generate_with_timing


def model_options(
    mode, target, draft, max_model_len, concurrency, seed, history_config=None, disable_qknorm_rope=False
):
    if mode not in ("ar", "full", "window") and not (mode == "anchors" and history_config is not None):
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
    if mode == "anchors":
        options["additional_config"] = {"research_history_kv": history_config}
    if disable_qknorm_rope:
        options.setdefault("additional_config", {})["ascend_compilation_config"] = {"fuse_qknorm_rope": False}
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
    if reference is not None and reference.get("stop_at_eos", False) != args.stop_at_eos:
        raise ValueError("candidate and AR reference must use the same EOS policy")
    if reference is not None and reference.get("disable_qknorm_rope", False) != args.disable_qknorm_rope:
        raise ValueError("candidate and AR reference must use the same fusion configuration")
    if reference is not None and reference.get("target_eager", False) != args.target_eager:
        raise ValueError("candidate and AR reference must use the same target execution mode")
    if reference is not None and (
        reference["prompt_sha256"] != prompt_hash or reference["output_tokens"] != args.output_tokens
    ):
        raise ValueError("AR reference and candidate must use identical prompts and output length")

    from vllm import LLM, SamplingParams
    from vllm.config import CompilationConfig
    from vllm.sampling_params import RequestOutputKind

    import vllm_ascend

    proposer_path = Path(vllm_ascend.__file__).parent / "spec_decode/llm_base_proposer.py"
    proposer_source = proposer_path.read_text(encoding="utf-8")
    if args.mode in ("window", "anchors") and "from paged_history import begin_dspark_history" not in proposer_source:
        raise ValueError("install the validated draft-read overlay before running window or anchors")

    history_config = None
    if args.mode == "anchors":
        from paged_history import HistoryConfig

        history_config = vars(
            HistoryConfig(args.local_tokens, args.anchor_tokens, args.history_policy, not args.no_cache)
        )
        if not all(item.get("query_token_ids") for item in data) and args.history_policy == "bm25":
            raise ValueError("historical BM25 requires question token IDs for every request")
    options = model_options(
        args.mode,
        args.target,
        args.draft,
        longest + args.output_tokens + 8,
        args.concurrency,
        args.seed,
        history_config=history_config,
        disable_qknorm_rope=args.disable_qknorm_rope,
    )
    options["compilation_config"] = CompilationConfig(
        cudagraph_mode="NONE" if args.target_eager else "FULL_DECODE_ONLY"
    )
    if args.target_eager:
        options["enforce_eager"] = True
    llm = LLM(**options)
    sampling = SamplingParams(
        temperature=0, max_tokens=args.output_tokens, ignore_eos=not args.stop_at_eos, seed=args.seed
    )
    if args.mode == "anchors":
        sampling = [
            SamplingParams(
                temperature=0,
                max_tokens=args.output_tokens,
                ignore_eos=not args.stop_at_eos,
                seed=args.seed,
                extra_args={"history_query_token_ids": item.get("query_token_ids", [])},
            )
            for item in data
        ]
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
        "stop_at_eos": args.stop_at_eos,
        "timing_method": args.timing_method,
        "history_config": history_config,
        "disable_qknorm_rope": args.disable_qknorm_rope,
        "target_eager": args.target_eager,
        "packages": {name: metadata.version(name) for name in ("vllm", "vllm-ascend", "torch", "torch-npu")},
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "proposer_sha256": hashlib.sha256(proposer_path.read_bytes()).hexdigest(),
        "target_config_sha256": hashlib.sha256((args.target / "config.json").read_bytes()).hexdigest(),
        "draft_config_sha256": hashlib.sha256((args.draft / "config.json").read_bytes()).hexdigest()
        if args.mode != "ar"
        else None,
        "runs": [],
    }
    for iteration in range(args.repeats):
        before = spec_counters(llm)
        observed_timings = None
        if args.timing_method == "emission":
            responses, observed_timings, seconds = generate_with_timing(
                llm.llm_engine, prompts, sampling, RequestOutputKind.CUMULATIVE, f"measurement-{iteration}"
            )
        else:
            start = time.perf_counter()
            responses = llm.generate(prompts, sampling, use_tqdm=False)
            seconds = time.perf_counter() - start
        after = spec_counters(llm)
        output_ids = [list(response.outputs[0].token_ids) for response in responses]
        valid_lengths = all(
            0 < len(tokens) <= args.output_tokens if args.stop_at_eos else len(tokens) == args.output_tokens
            for tokens in output_ids
        )
        if len(output_ids) != len(data) or not valid_lengths:
            raise ValueError("generation did not produce the requested fixed workload")
        matched = output_ids == reference["runs"][0]["output_token_ids"] if reference is not None else None
        tpots = []
        for response, tokens in zip(responses, output_ids, strict=True):
            metrics = getattr(response, "metrics", None)
            tpots.append(
                request_tpot_ms(
                    getattr(metrics, "first_token_time", None), getattr(metrics, "finished_time", None), len(tokens)
                )
            )
        if observed_timings is not None:
            tpots = [timing["tpot_ms"] for timing in observed_timings]
        report["runs"].append(
            {
                "iteration": iteration,
                "wall_seconds": seconds,
                "output_tokens_per_second_including_prefill": sum(map(len, output_ids)) / seconds,
                "request_tpot_ms": tpots,
                "observed_request_timing": observed_timings,
                "output_token_ids": output_ids,
                "matches_ar_reference": matched,
                "speculative_counter_deltas": {name: value - before.get(name, 0) for name, value in after.items()},
            }
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if matched is False:
            raise AssertionError("greedy output tokens differ from AR reference; raw candidate output was preserved")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["ar", "full", "window", "anchors"], required=True)
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
    parser.add_argument(
        "--stop-at-eos", action="store_true", help="quality regression; fixed-length timing ignores EOS"
    )
    parser.add_argument("--timing-method", choices=["emission", "generate"], default="emission")
    parser.add_argument(
        "--disable-qknorm-rope", action="store_true", help="controlled configuration for the 910B2 fusion failure"
    )
    parser.add_argument("--history-policy", choices=["bm25", "uniform", "sink", "recent"], default="bm25")
    parser.add_argument("--local-tokens", type=int, default=2048)
    parser.add_argument("--anchor-tokens", type=int, default=128)
    parser.add_argument(
        "--no-cache", action="store_true", help="include repeated historical selection in every draft step"
    )
    parser.add_argument(
        "--target-eager", action="store_true", help="diagnose compiled-path failures with an explicit eager control"
    )
    parsed = parser.parse_args()
    if min(parsed.concurrency, parsed.output_tokens, parsed.repeats, parsed.warmup) < 1:
        parser.error("concurrency, output tokens, repeats and warmup must be positive")
    main(parsed)
