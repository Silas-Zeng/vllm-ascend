"""Validate and summarize the complete, unprofiled 18-configuration study."""

import argparse
import csv
import hashlib
import json
import math
import statistics
from pathlib import Path

CONTEXTS = (4096, 16384, 24576)
BATCHES = (1, 4)
METHODS = ("ar", "full", "window")
NATIVE_PROPOSER_SHA = "25f0e85ea44291448b8a1b91408d6e4f8c80636646b603f78dd1c3a8fdece01f"
WINDOW_PROPOSER_SHA = "32b2eb3e883f434d39f058b06868ee0e88309d3e99b1144fa23b034b5e8ffc3e"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def distribution(values, allow_zero=False):
    require(
        bool(values) and all(math.isfinite(v) and (v >= 0 if allow_zero else v > 0) for v in values), "invalid samples"
    )
    quartiles = statistics.quantiles(values, n=4, method="inclusive") if len(values) > 1 else values * 3
    return {
        "n": len(values),
        "median": statistics.median(values),
        "q25": quartiles[0],
        "q75": quartiles[2],
        "min": min(values),
        "max": max(values),
    }


def common_prefix(left, right):
    return next((i for i, pair in enumerate(zip(left, right)) if pair[0] != pair[1]), min(len(left), len(right)))


def validate_report(report, method, context, batch, expected_timing_method=None, profiled=False):
    """Check immutable workload, raw comparisons, counters and native clock contracts."""
    require(report["mode"] == method, "method mismatch")
    require(report["concurrency"] == batch and report["prompt_lengths"] == [context] * batch, "input mismatch")
    require(report["output_tokens"] == 512 and not report["stop_at_eos"], "output contract changed")
    require(report["seed"] == 42 and report["max_model_len"] == 32768, "seed/model limit changed")
    require(
        report["history_config"] is None and not report["disable_qknorm_rope"], "non-native algorithm/fusion override"
    )
    timing_method = report["timing_method"]
    require(timing_method in ("emission", "native_stats"), "unknown timing method")
    require((report["profile_dir"] is not None) == profiled, "profile/nonprofile contract changed")
    if expected_timing_method is not None:
        require(timing_method == expected_timing_method, "different timing entry")
    policy = report.get("native_control_comparison_policy", "strict")
    require(policy in ("strict", "record_variation"), "unknown control comparison policy")
    options = report["requested_model_options"]
    require(options["dtype"] == "bfloat16" and not options["enable_prefix_caching"], "dtype/cache changed")
    require(
        options["tensor_parallel_size"] == 1 and options["gpu_memory_utilization"] == 0.8, "parallel/memory changed"
    )
    require(options["max_num_seqs"] == batch and not report["target_eager"], "batch/execution changed")
    expected_sha = WINDOW_PROPOSER_SHA if method == "window" else NATIVE_PROPOSER_SHA
    require(report["proposer_sha256"] == expected_sha, "proposer source drift")
    if method == "window":
        require(options["additional_config"] == {"draft_window_size": 2176}, "window configuration changed")
        require(report["window_implementation"] == "per_group_window_baseline", "window wiring changed")
    else:
        require(not options.get("additional_config"), "unexpected target/draft configuration override")
    schedule = report["resolved_scheduler_config"]
    require(schedule["max_num_scheduled_tokens"] == 4096, "actual scheduler budget changed")
    require(schedule["max_num_batched_tokens"] == 4096 + (0 if method == "ar" else 6 * batch), "slot reserve changed")
    require(schedule["enable_chunked_prefill"] and schedule["async_scheduling"], "execution flags changed")
    require(report["resolved_compilation_config"]["cudagraph_mode"] == "FULL_DECODE_ONLY", "target graph mode changed")
    captures = (
        ([1] if batch == 1 else [1, 2, 4])
        if method == "ar"
        else ([1, 2, 4, 8] if batch == 1 else [1, 2, 4, 8, 16, 24, 32])
    )
    require(
        report["resolved_compilation_config"]["cudagraph_capture_sizes"] == captures, "graph capture geometry changed"
    )
    spec = options.get("speculative_config")
    require(
        spec is None
        if method == "ar"
        else (spec["method"] == "dspark" and spec["num_speculative_tokens"] == 7 and spec["enforce_eager"]),
        "draft settings changed",
    )
    require(
        len(report["warmup_runs"]) == (3 if profiled else 10) and len(report["runs"]) == (1 if profiled else 10),
        "incomplete repetitions",
    )
    control = report["generate_control_output_token_ids"]
    require(len(control) == batch and all(len(tokens) == 512 for tokens in control), "invalid native control")
    for iteration, run in enumerate(report["runs"]):
        require(run["iteration"] == iteration, "missing or duplicate iteration")
        matches_control = run["output_token_ids"] == control
        require(run["matches_generate_control"] == matches_control, "within-method instability flag contradicts tokens")
        require(policy != "strict" or matches_control, "within-method instability under strict policy")
        require(len(run["output_token_ids"]) == batch, "missing request output")
        require(all(len(tokens) == 512 for tokens in run["output_token_ids"]), "wrong actual output length")
        require(len(run["observed_request_timing"]) == batch, "missing emission timings")
        require(math.isfinite(run["wall_seconds"]) and run["wall_seconds"] > 0, "invalid wall time")
        require(
            math.isclose(run["output_tokens_per_second_including_prefill"], batch * 512 / run["wall_seconds"]),
            "throughput formula mismatch",
        )
        for request, timing in enumerate(run["observed_request_timing"]):
            if timing_method == "native_stats":
                state = timing["native_request_state_stats"]
                finished = timing["native_finished_request_stats"]
                require(
                    state["num_generation_tokens"] == finished["num_generation_tokens"] == 512,
                    "native token count mismatch",
                )
                require(not finished["is_corrupted"], "native engine flagged corruption")
                require(
                    0 < state["queued_ts"] <= state["scheduled_ts"] <= state["first_token_ts"] < state["last_token_ts"],
                    "invalid core monotonic timestamps",
                )
                decode = state["last_token_ts"] - state["first_token_ts"]
                require(math.isclose(finished["decode_time"], decode), "native decode interval mismatch")
                require(math.isclose(timing["tpot_ms"], decode * 1000 / 511), "native TPOT formula mismatch")
                require(
                    math.isclose(timing["ttft_ms"], state["first_token_latency"] * 1000),
                    "native frontend TTFT mismatch",
                )
                require(math.isclose(timing["latency_ms"], finished["e2e_latency"] * 1000), "native latency mismatch")
                require(math.isclose(timing["tpot_ms"], run["request_tpot_ms"][request]), "TPOT copies disagree")
                continue
            events = timing["events"]
            require(events and events[-1]["output_tokens"] == 512 and events[-1]["finished"], "incomplete emissions")
            require(events[0]["output_tokens"] == timing["first_output_tokens"], "first emission mismatch")
            require(
                all(
                    a["time_ms"] < b["time_ms"] and a["output_tokens"] < b["output_tokens"]
                    for a, b in zip(events, events[1:])
                ),
                "nonmonotonic emissions",
            )
            expected = (events[-1]["time_ms"] - events[0]["time_ms"]) / (512 - timing["first_output_tokens"])
            require(math.isclose(expected, timing["tpot_ms"], rel_tol=1e-9), "TPOT formula mismatch")
            require(math.isclose(timing["tpot_ms"], run["request_tpot_ms"][request]), "TPOT copies disagree")
        counters = run["speculative_counter_deltas"]
        if method != "ar":
            drafts = counters["vllm:spec_decode_num_drafts"]
            proposed = counters["vllm:spec_decode_num_draft_tokens"]
            accepted = counters["vllm:spec_decode_num_accepted_tokens"]
            require(drafts > 0 and proposed == drafts * 7 and 0 <= accepted <= proposed, "invalid speculative counters")


def summarize_report(report, reference, prompts, tokenizer):
    wall = [r["wall_seconds"] for r in report["runs"]]
    throughput = [r["output_tokens_per_second_including_prefill"] for r in report["runs"]]
    # Reduce requests within each batch first: ten independent batch repetitions,
    # not forty independent timing samples for concurrency four.
    tpot = [statistics.median(r["request_tpot_ms"]) for r in report["runs"]]
    ttft = [statistics.median(t["ttft_ms"] for t in r["observed_request_timing"]) for r in report["runs"]]
    reference_outputs = reference["runs"][0]["output_token_ids"]
    quality = []
    for request, (expected_tokens, prompt) in enumerate(zip(reference_outputs, prompts)):
        outputs = [run["output_token_ids"][request] for run in report["runs"]]
        texts = [tokenizer.decode(tokens, skip_special_tokens=True) for tokens in outputs]
        code = prompt["expected_answer"]
        variants = {}
        for iteration, (tokens, text) in enumerate(zip(outputs, texts)):
            key = tuple(tokens)
            if key not in variants:
                variants[key] = {"decoded_output": text, "iterations": [], "output_token_ids": tokens}
            variants[key]["iterations"].append(iteration)
        prefixes = [common_prefix(tokens, expected_tokens) for tokens in outputs]
        exact = [tokens == expected_tokens for tokens in outputs]
        hits = [code in text for text in texts]
        starts = [text.lstrip().startswith(code) for text in texts]
        quality.append(
            {
                "request": request,
                "ar_exact_match": all(exact),
                "ar_exact_runs": sum(exact),
                "ar_common_prefix_min": min(prefixes),
                "ar_common_prefix_max": max(prefixes),
                "ar_common_prefix_by_run": prefixes,
                "recovery_code_found": all(hits),
                "recovery_code_found_runs": sum(hits),
                "starts_with_recovery_code": all(starts),
                "starts_with_recovery_code_runs": sum(starts),
                "native_control_matching_runs": sum(
                    tokens == report["generate_control_output_token_ids"][request] for tokens in outputs
                ),
                "output_variants": list(variants.values()),
            }
        )
    sums = {}
    for run in report["runs"]:
        for key, value in run["speculative_counter_deltas"].items():
            sums[key] = sums.get(key, 0) + value
    drafts = sums.get("vllm:spec_decode_num_drafts", 0)
    proposed = sums.get("vllm:spec_decode_num_draft_tokens", 0)
    accepted = sums.get("vllm:spec_decode_num_accepted_tokens", 0)
    return {
        "method": report["mode"],
        "context": report["prompt_lengths"][0],
        "concurrency": report["concurrency"],
        "wall_seconds": distribution(wall),
        "warmup_wall_seconds": [run["wall_seconds"] for run in report["warmup_runs"]],
        "measured_wall_seconds": wall,
        "measured_last5_vs_first5_wall_ratio": statistics.median(wall[5:]) / statistics.median(wall[:5]),
        "throughput_tokens_s": distribution(throughput),
        "batch_median_tpot_ms": distribution(tpot),
        "batch_median_ttft_ms": distribution(ttft),
        "batch_median_native_phase_ms": {
            phase: distribution(
                [statistics.median(t[phase] for t in run["observed_request_timing"]) for run in report["runs"]],
                allow_zero=phase == "queued_ms",
            )
            for phase in ("queued_ms", "prefill_ms", "decode_ms", "inference_ms")
            if phase in report["runs"][0]["observed_request_timing"][0]
        },
        "pooled_counter_deltas": sums,
        "accepted_draft_tokens_per_round": accepted / drafts if drafts else None,
        "nominal_accepted_length": 1 + accepted / drafts if drafts else None,
        "draft_token_acceptance_rate": accepted / proposed if proposed else None,
        "observed_output_tokens_per_draft_round": 10 * report["concurrency"] * 512 / drafts if drafts else None,
        "ar_exact_requests": sum(q["ar_exact_match"] for q in quality),
        "ar_exact_request_runs": sum(q["ar_exact_runs"] for q in quality),
        "code_found_requests": sum(q["recovery_code_found"] for q in quality),
        "code_found_request_runs": sum(q["recovery_code_found_runs"] for q in quality),
        "starts_with_code_requests": sum(q["starts_with_recovery_code"] for q in quality),
        "native_control_matching_batches": sum(run["matches_generate_control"] for run in report["runs"]),
        "unique_batch_outputs": len(
            {tuple(tuple(tokens) for tokens in run["output_token_ids"]) for run in report["runs"]}
        ),
        "native_control_comparison_policy": report.get("native_control_comparison_policy", "strict"),
        "quality": quality,
        "startup_worker_info": report["worker_info"],
        "per_request_metrics": [
            {
                "request": index,
                **{
                    key: distribution(
                        [r["observed_request_timing"][index][key] for r in report["runs"]],
                        allow_zero=key == "queued_ms",
                    )
                    for key in (
                        "ttft_ms",
                        "tpot_ms",
                        "latency_ms",
                        "queued_ms",
                        "prefill_ms",
                        "decode_ms",
                        "inference_ms",
                    )
                    if key in report["runs"][0]["observed_request_timing"][index]
                },
            }
            for index in range(report["concurrency"])
        ],
    }


def analyze(directory, prompt_directory, tokenizer):
    reports = {}
    sources = {}
    for batch in BATCHES:
        for context in CONTEXTS:
            prompt_path = prompt_directory / f"prompts_long_c{context}_b{batch}.json"
            sources[str(prompt_path)] = sha256(prompt_path)
            for method in METHODS:
                label = f"{method}_c{context}_b{batch}"
                path = directory / (label + ".json")
                report = json.loads(path.read_text(encoding="utf-8"))
                validate_report(report, method, context, batch, expected_timing_method="native_stats")
                require(report["prompt_sha256"] == sha256(prompt_path), "prompt checksum mismatch")
                sources[str(path)] = sha256(path)
                if method != "ar":
                    audit_path = directory / (label + "_audit.json")
                    audit = json.loads(audit_path.read_text(encoding="utf-8"))
                    require(
                        audit["passed"] and audit["checked_request_group_calls"] > 0, "draft read audit did not pass"
                    )
                    sources[str(audit_path)] = sha256(audit_path)
                reports[label] = report
    reference = next(iter(reports.values()))
    for report in reports.values():
        for key in ("packages", "runner_sha256", "target_config_sha256"):
            require(report[key] == reference[key], "matrix source drift: " + key)
        require(report["native_nan_check_enabled"] == reference["native_nan_check_enabled"], "NaN check setting drift")
    draft_hashes = {report["draft_config_sha256"] for report in reports.values() if report["mode"] != "ar"}
    require(len(draft_hashes) == 1 and None not in draft_hashes, "draft configuration drift")
    summaries = []
    for batch in BATCHES:
        for context in CONTEXTS:
            ar = reports[f"ar_c{context}_b{batch}"]
            prompts = json.loads((prompt_directory / f"prompts_long_c{context}_b{batch}.json").read_text())
            require(
                len(prompts) == batch and all(len(p["prompt_token_ids"]) == context for p in prompts),
                "input file length mismatch",
            )
            ar_median = statistics.median(r["wall_seconds"] for r in ar["runs"])
            for method in METHODS:
                report = reports[f"{method}_c{context}_b{batch}"]
                summary = summarize_report(report, ar, prompts, tokenizer)
                summary["wall_speed_ratio_vs_ar"] = ar_median / summary["wall_seconds"]["median"]
                for run in report["runs"]:
                    if method != "ar":
                        require(
                            run["matches_ar_reference"]
                            == (run["output_token_ids"] == ar["runs"][0]["output_token_ids"]),
                            "saved reference flag disagrees with tokens",
                        )
                summaries.append(summary)
    return {
        "scope": "complete unprofiled context matrix; performance ratios are not proof of lossless decoding",
        "source_sha256": sources,
        "configurations": summaries,
    }


def write_csv(result, output):
    fields = [
        "method",
        "context",
        "concurrency",
        "wall_median_s",
        "wall_q25_s",
        "wall_q75_s",
        "throughput_median_tokens_s",
        "tpot_median_ms",
        "ttft_median_ms",
        "nominal_accepted_length",
        "draft_token_acceptance_rate",
        "wall_speed_ratio_vs_ar",
        "ar_exact_requests",
        "code_found_requests",
        "ar_exact_request_runs",
        "code_found_request_runs",
        "native_control_matching_batches",
        "unique_batch_outputs",
    ]
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in result["configurations"]:
            writer.writerow(
                {
                    **{field: item[field] for field in fields if field in item},
                    "wall_median_s": item["wall_seconds"]["median"],
                    "wall_q25_s": item["wall_seconds"]["q25"],
                    "wall_q75_s": item["wall_seconds"]["q75"],
                    "throughput_median_tokens_s": item["throughput_tokens_s"]["median"],
                    "tpot_median_ms": item["batch_median_tpot_ms"]["median"],
                    "ttft_median_ms": item["batch_median_ttft_ms"]["median"],
                }
            )


def plot(result, output):
    """Plot the full matrix, its variability, and every recorded warmup/measurement."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"ar": "#566573", "full": "#1677b8", "window": "#de8130"}
    labels = {"ar": "AR", "full": "DSpark Full KV", "window": "DSpark Window 2176 (wiring fix)"}
    for metric, title, units in [
        ("wall_seconds", "Batch completion time (prefill included)", "seconds"),
        ("batch_median_tpot_ms", "Native time per output token after the first token", "ms/token"),
        ("batch_median_ttft_ms", "Time to first output token", "ms"),
        ("throughput_tokens_s", "Aggregate output throughput (prefill included)", "tokens/s"),
    ]:
        fig, axes = plt.subplots(1, 2, figsize=(12, 4.3), constrained_layout=True)
        for axis, batch in zip(axes, BATCHES):
            for method in METHODS:
                rows = [q for q in result["configurations"] if q["method"] == method and q["concurrency"] == batch]
                y = [q[metric]["median"] for q in rows]
                errors = [
                    [v - q[metric]["q25"] for v, q in zip(y, rows)],
                    [q[metric]["q75"] - v for v, q in zip(y, rows)],
                ]
                axis.errorbar(
                    [4, 16, 24], y, yerr=errors, label=labels[method], color=colors[method], marker="o", capsize=4
                )
            axis.set(title=f"Concurrency {batch}", xlabel="Input length (Ki tokens)", ylabel=units, xticks=[4, 16, 24])
            axis.grid(alpha=0.2)
            axis.set_ylim(bottom=0)
        axes[0].legend(fontsize=8)
        fig.suptitle(
            title + "\n10 warmups + 10 runs; median and IQR; BF16, K=7; output variation recorded",
            fontsize=11,
        )
        for suffix in ("png", "svg"):
            fig.savefig(output / f"context_{metric}.{suffix}", dpi=170)
        plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.3), constrained_layout=True)
    for axis, batch in zip(axes, BATCHES):
        for method in ("full", "window"):
            rows = [q for q in result["configurations"] if q["method"] == method and q["concurrency"] == batch]
            axis.plot(
                [4, 16, 24],
                [q["nominal_accepted_length"] for q in rows],
                label=labels[method],
                color=colors[method],
                marker="o",
            )
        axis.set(
            title=f"Concurrency {batch}",
            xlabel="Input length (Ki tokens)",
            ylabel="1 + accepted draft tokens / draft rounds",
            xticks=[4, 16, 24],
            ylim=(1, 8),
        )
        axis.grid(alpha=0.2)
    axes[0].legend(fontsize=8)
    fig.suptitle(
        "Mean accepted length including bonus token\n"
        "Pooled counters over 10 measured runs; final-round truncation may affect emitted tokens",
        fontsize=11,
    )
    for suffix in ("png", "svg"):
        fig.savefig(output / f"context_acceptance.{suffix}", dpi=170)
    plt.close(fig)
    fig, axes = plt.subplots(3, 2, figsize=(12, 10), constrained_layout=True)
    for row, context in enumerate(CONTEXTS):
        for column, batch in enumerate(BATCHES):
            axis = axes[row, column]
            axis.axvspan(0.5, 10.5, color="#eef0f2")
            for method in METHODS:
                item = next(
                    q
                    for q in result["configurations"]
                    if q["method"] == method and q["context"] == context and q["concurrency"] == batch
                )
                values = item["warmup_wall_seconds"] + item["measured_wall_seconds"]
                distribution(values)
                axis.plot(range(1, 21), values, color=colors[method], label=labels[method], marker=".")
            axis.axvline(10.5, color="#87929c", linestyle="--", linewidth=0.8)
            axis.set(
                title=f"{context // 1024}K, concurrency {batch}",
                xlabel="Warmups 1–10 | Measurements 1–10",
                ylabel="Batch completion time (s)",
                xlim=(0.5, 20.5),
                ylim=(0, None),
            )
            axis.grid(alpha=0.2)
    axes[0, 0].legend(fontsize=8)
    fig.suptitle(
        "Every warmup and measured batch, in recorded order\n"
        "A separate native control runs between the two phases and is excluded from this plot",
        fontsize=11,
    )
    for suffix in ("png", "svg"):
        fig.savefig(output / f"context_repetitions.{suffix}", dpi=170)
    plt.close(fig)
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    for row, phase in enumerate(("prefill_ms", "decode_ms")):
        for column, batch in enumerate(BATCHES):
            axis = axes[row, column]
            for method in METHODS:
                rows = [q for q in result["configurations"] if q["method"] == method and q["concurrency"] == batch]
                values = [q["batch_median_native_phase_ms"][phase] for q in rows]
                y = [v["median"] / 1000 for v in values]
                error = [
                    [(v["median"] - v["q25"]) / 1000 for v in values],
                    [(v["q75"] - v["median"]) / 1000 for v in values],
                ]
                axis.errorbar(
                    [4, 16, 24], y, yerr=error, label=labels[method], color=colors[method], marker="o", capsize=4
                )
            axis.set(
                title=f"Concurrency {batch}: native {phase.removesuffix('_ms')} interval",
                xlabel="Input length (Ki tokens)",
                ylabel="seconds / request (batch median)",
                xticks=[4, 16, 24],
                ylim=(0, None),
            )
            axis.grid(alpha=0.2)
    axes[0, 0].legend(fontsize=8)
    fig.suptitle(
        "Semantic request phases from unprofiled native statistics\n"
        "10 runs; intervals include scheduling and waits, and are not cumulative NPU task durations",
        fontsize=11,
    )
    for suffix in ("png", "svg"):
        fig.savefig(output / f"context_request_phases.{suffix}", dpi=170)
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--figures", type=Path)
    args = parser.parse_args()
    from tokenizers import Tokenizer

    result = analyze(args.directory, args.prompts, Tokenizer.from_file(str(args.tokenizer)))
    result["tokenizer_sha256"] = sha256(args.tokenizer)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "context_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_csv(result, args.output / "context_summary.csv")
    figures = args.figures or args.output
    figures.mkdir(parents=True, exist_ok=True)
    plot(result, figures)
