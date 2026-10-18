"""Separate C4 AR native repeatability from frontend measurement effects."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from emission_timing import generate_with_timing
from native_emission_timing import generate_native_with_timing
from run_vllm_baseline import model_options


def main(args):
    from vllm import LLM, SamplingParams
    from vllm.config import CompilationConfig
    from vllm.sampling_params import RequestOutputKind

    data = json.loads(args.prompts.read_text())
    prompts = [{"prompt_token_ids": item["prompt_token_ids"]} for item in data]
    options = model_options("ar", args.target, None, 32768, len(data), 42)
    options.update(
        max_num_batched_tokens=4096,
        max_num_scheduled_tokens=4096,
        enable_chunked_prefill=True,
        async_scheduling=True,
        compilation_config=CompilationConfig(cudagraph_mode="FULL_DECODE_ONLY"),
    )
    llm = LLM(**options)
    sampling = SamplingParams(temperature=0, max_tokens=512, ignore_eos=True, seed=42)
    report = {
        "scope": "diagnostic only, not formal timing samples",
        "warmup": args.warmup,
        "prompt_sha256": hashlib.sha256(args.prompts.read_bytes()).hexdigest(),
        "options": json.loads(json.dumps(options, default=str)),
        "sources": {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in (
                "probe_native_timing.py",
                "native_emission_timing.py",
                "emission_timing.py",
                "run_vllm_baseline.py",
            )
        },
        "runs": [],
    }
    for iteration, mode in enumerate(
        ["native_warmup"] * args.warmup
        + ["native"] * 3
        + ["direct_engine"] * 2
        + ["native_observer"] * 3
        + ["native_after"] * 2
    ):
        started = time.perf_counter()
        timings = None
        if mode == "direct_engine":
            outputs, timings, seconds = generate_with_timing(
                llm.llm_engine, prompts, sampling, RequestOutputKind.CUMULATIVE, f"probe-{iteration}"
            )
        elif mode == "native_observer":
            outputs, timings, seconds = generate_native_with_timing(llm, prompts, sampling)
        else:
            outputs = llm.generate(prompts, sampling, use_tqdm=False)
            seconds = time.perf_counter() - started
        tokens = [list(response.outputs[0].token_ids) for response in outputs]
        if len(tokens) != len(data) or any(len(row) != 512 for row in tokens):
            raise ValueError("diagnostic did not generate requested workload")
        report["runs"].append(
            {
                "iteration": iteration,
                "mode": mode,
                "wall_seconds": seconds,
                "output_token_ids": tokens,
                "observed_request_timing": timings,
            }
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps({"phase": mode, "completed": iteration + 1, "total": args.warmup + 10, "seconds": seconds}),
            flush=True,
        )
    reference = next(run for run in report["runs"] if run["mode"] == "native")["output_token_ids"]
    for run in report["runs"]:
        run["matches_first_native_by_request"] = [
            left == right for left, right in zip(run["output_token_ids"], reference)
        ]
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmup", type=int, default=3)
    main(parser.parse_args())
