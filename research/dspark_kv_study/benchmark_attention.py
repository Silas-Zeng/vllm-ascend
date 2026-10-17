"""Synchronized NPU component experiment, NOT a DSpark/vLLM throughput test."""

import argparse
import hashlib
import json
import math
import platform
import random
import statistics
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from history_policy import BM25HistoryIndex, select_positions


def summarize(samples):
    ordered = sorted(samples)
    return {
        "median_ms": statistics.median(ordered),
        "p10_ms": ordered[math.floor((len(ordered) - 1) * 0.1)],
        "p90_ms": ordered[math.ceil((len(ordered) - 1) * 0.9)],
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
    }


def revision():
    proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def trace_events(payload):
    """TorchNPU exports a list; other Chrome trace exporters use a wrapper."""
    events = payload if isinstance(payload, list) else payload["traceEvents"]
    if not isinstance(events, list):
        raise ValueError("invalid Chrome trace event list")
    return events


def run(args):
    # Lazy imports keep the policy and its tests usable on machines without torch.
    import torch
    import torch_npu

    if not torch.npu.is_available():
        raise RuntimeError("An available NPU is required; CPU timing must not be mislabeled")
    torch.npu.set_device(args.device)
    torch.set_num_threads(8)
    rng = random.Random(args.seed)
    report = {
        "schema_version": 1,
        "scope": "synthetic_dense_attention_component_only",
        "is_end_to_end_dspark": False,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_revision": revision(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "policy_sha256": hashlib.sha256(Path(__file__).with_name("history_policy.py").read_bytes()).hexdigest(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "torch": torch.__version__,
            "torch_npu": torch_npu.__version__,
            "device_name": torch.npu.get_device_name(args.device),
            "cpu_threads": torch.get_num_threads(),
        },
        "configuration": vars(args) | {"output": str(args.output.name)},
        "method": {
            "attention": "unfused torch.matmul -> FP32 softmax -> BF16 matmul; no causal mask (cached history only)",
            "timing": "perf_counter; NPU synchronized before/after; paired randomized path order per repeat",
            "online_index": "BM25 request index constructed outside timing; ranking and index transfer inside timing",
            "cached_index": (
                "only historical anchor positions cached; rebuild local-window arange and concatenate on device, "
                "then gather K/V inside timing"
            ),
            "window_tokens": 2176,
            "anchor_local_tokens": 2048,
            "anchor_history_tokens": 128,
            "query_positions": 8,
            "heads": 8,
            "head_dimension": 128,
            "dtype": "bfloat16",
            "correctness_atol": 0.02,
            "correctness_rtol": 0.02,
        },
        "cases": [],
    }

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    for batch in args.batches:
        for context in args.contexts:
            torch.manual_seed(args.seed + context + batch)
            query_cpu = torch.randn(batch, 8, 8, 128, dtype=torch.bfloat16)
            key_cpu = torch.randn(batch, 8, context, 128, dtype=torch.bfloat16)
            value_cpu = torch.randn_like(key_cpu)
            query = query_cpu.to("npu")
            key = key_cpu.to("npu")
            value = value_cpu.to("npu")
            tokens = [rng.randrange(1024) for _ in range(context)]
            tokens[256:384] = [4096] * 128
            index = BM25HistoryIndex(tokens)
            question = [4096, 13, 29]
            selected = select_positions(index, question, context)
            positions_cpu = torch.tensor(selected.positions, dtype=torch.long)
            anchor_positions = [position for position in selected.positions if position < selected.local_start]
            anchors_npu = torch.tensor(anchor_positions, dtype=torch.long).to("npu")

            def attention(q, k, v):
                scores = torch.matmul(q, k.transpose(-1, -2)) / math.sqrt(q.shape[-1])
                probabilities = torch.softmax(scores.float(), dim=-1).to(q.dtype)
                return torch.matmul(probabilities, v)

            def full_kv(query=query, key=key, value=value):
                return attention(query, key, value)

            def window_2176(query=query, key=key, value=value):
                return attention(query, key[:, :, -2176:, :], value[:, :, -2176:, :])

            def anchors_cached(
                query=query,
                key=key,
                value=value,
                anchors_npu=anchors_npu,
                local_start=selected.local_start,
                context=context,
            ):
                # Only the remote anchors are request-cacheable. The local window
                # must be rebuilt as decode advances; include its preparation cost.
                local_indices = torch.arange(local_start, context, dtype=torch.long, device=query.device)
                indices = torch.cat((anchors_npu, local_indices))
                return attention(query, key.index_select(2, indices), value.index_select(2, indices))

            def anchors_online(query=query, key=key, value=value, index=index, question=question, context=context):
                current = select_positions(index, question, context)
                indices = torch.tensor(current.positions, dtype=torch.long).to("npu")
                return attention(query, key.index_select(2, indices), value.index_select(2, indices))

            paths = {
                "full_kv": full_kv,
                "window_2176": window_2176,
                "anchors_online": anchors_online,
                "anchors_cached": anchors_cached,
            }
            correctness = {}
            with torch.inference_mode():
                for name, operation in paths.items():
                    if name == "full_kv":
                        reference_key, reference_value = key_cpu, value_cpu
                    elif name == "window_2176":
                        reference_key, reference_value = key_cpu[:, :, -2176:, :], value_cpu[:, :, -2176:, :]
                    else:
                        reference_key = key_cpu.index_select(2, positions_cpu)
                        reference_value = value_cpu.index_select(2, positions_cpu)
                    reference = attention(query_cpu.float(), reference_key.float(), reference_value.float())
                    actual = operation().cpu().float()
                    torch.testing.assert_close(actual, reference, atol=0.02, rtol=0.02)
                    correctness[name] = {"passed": True, "max_absolute_error": (actual - reference).abs().max().item()}
                    for _ in range(args.warmup):
                        operation()
                    torch.npu.synchronize()
                samples = {name: [] for name in paths}
                order_records = []
                for _ in range(args.repeats):
                    order = list(paths)
                    rng.shuffle(order)
                    order_records.append(order)
                    for name in order:
                        torch.npu.synchronize()
                        start = time.perf_counter()
                        paths[name]()
                        torch.npu.synchronize()
                        samples[name].append((time.perf_counter() - start) * 1000)
            case = {
                "batch": batch,
                "context_tokens": context,
                "anchor_blocks": list(selected.anchor_blocks),
                "selected_tokens": len(selected.positions),
                "correctness": correctness,
                "path_order_per_repeat": order_records,
                "paths": {name: summarize(values) | {"samples_ms": values} for name, values in samples.items()},
            }
            report["cases"].append(case)
            save()
            print(
                json.dumps(
                    {
                        "batch": batch,
                        "context": context,
                        "median_ms": {n: c["median_ms"] for n, c in case["paths"].items()},
                    }
                ),
                flush=True,
            )

            if args.profile and batch == max(args.batches) and context == max(args.contexts):
                from torch_npu.profiler import ProfilerActivity, profile, schedule

                trace = args.output.with_suffix(".trace.json")
                with (
                    profile(
                        activities=[ProfilerActivity.CPU, ProfilerActivity.NPU],
                        schedule=schedule(wait=0, warmup=1, active=1, repeat=1),
                        record_shapes=True,
                    ) as prof,
                    torch.inference_mode(),
                ):
                    for _ in range(2):
                        for name, operation in paths.items():
                            with torch.autograd.profiler.record_function("kv_study/" + name):
                                operation()
                                torch.npu.synchronize()
                        prof.step()
                prof.export_chrome_trace(str(trace))
                events = trace_events(json.loads(trace.read_text(encoding="utf-8")))
                complete_events = [event for event in events if event.get("ph") == "X"]
                report["profiling"] = {
                    "trace_file": trace.name,
                    "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
                    "scope": "separate untimed diagnostic run",
                    "duration_unit": "microseconds",
                    "categories": dict(Counter(event.get("cat", "unspecified") for event in complete_events)),
                    "user_ranges": [
                        {"name": event["name"], "duration_us": event["dur"]}
                        for event in complete_events
                        if event.get("name", "").startswith("kv_study/")
                    ],
                }
                save()
            query = key = value = anchors_npu = None
            query_cpu = key_cpu = value_cpu = None
            reference_key = reference_value = reference = actual = None
            torch.npu.empty_cache()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--contexts", type=int, nargs="+", default=[4096, 16384, 24576])
    parser.add_argument("--batches", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--profile", action="store_true")
    parsed = parser.parse_args()
    if parsed.repeats < 10 or parsed.warmup < 1 or min(parsed.contexts) < 2176 or min(parsed.batches) < 1:
        parser.error("need >=10 repeats, >=1 warmup, positive batches and contexts >=2176")
    run(parsed)
