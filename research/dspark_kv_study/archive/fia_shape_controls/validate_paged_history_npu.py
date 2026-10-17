"""Validate real paged FIA reads against selected absolute-token FP32 attention.

Run serially on an idle NPU. This is a numerical/backend gate, not a latency or
model-quality benchmark. Full physical caches and original metadata are retained.
"""

import argparse
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import torch
import torch_npu
from paged_history import HistoryConfig, PagedHistoryAdapter


def check_case(lengths, query_tokens, policy):
    page, query_heads, kv_heads, head_dim = 128, 8, 2, 128
    batch = len(lengths)
    columns = (max(lengths) + page - 1) // page
    generator = torch.Generator().manual_seed(12345)
    logical_key = torch.randn(batch, columns * page, kv_heads, head_dim, generator=generator).bfloat16()
    logical_value = torch.randn(batch, columns * page, kv_heads, head_dim, generator=generator).bfloat16()
    query = torch.randn(batch * query_tokens, query_heads, head_dim, generator=generator).bfloat16()
    physical_table = (torch.randperm(batch * columns, generator=generator) + 1).reshape(batch, columns)
    physical_key = torch.zeros(batch * columns + 1, page, kv_heads, head_dim, dtype=torch.bfloat16)
    physical_value = torch.zeros_like(physical_key)
    for row in range(batch):
        for logical_page, physical_page in enumerate(physical_table[row]):
            start = logical_page * page
            physical_key[physical_page] = logical_key[row, start : start + page]
            physical_value[physical_page] = logical_value[row, start : start + page]
    window = columns * page if policy == "full" else (2176 if policy == "window" else 2048)
    config = HistoryConfig(local_tokens=window, anchor_tokens=128 if policy == "bm25" else 0, policy="bm25")
    requests = {}
    for row, length in enumerate(lengths):
        prompt = [0] * (length - query_tokens)
        if len(prompt) >= 512:
            prompt[384:512] = [999] * 128
        requests[str(row)] = SimpleNamespace(
            prompt_token_ids=prompt, sampling_params=SimpleNamespace(extra_args={"history_query_token_ids": [999]})
        )
    original = SimpleNamespace(
        num_reqs=batch,
        seq_lens=torch.tensor(lengths, dtype=torch.int32, device="npu"),
        block_table_tensor=physical_table.to(device="npu", dtype=torch.int32),
        max_seq_len=max(lengths),
        slot_mapping=torch.arange(batch * query_tokens, dtype=torch.int32, device="npu"),
        positions=torch.arange(batch * query_tokens, dtype=torch.int64, device="npu") + 1000,
        causal=False,
    )
    candidate = copy.copy(original)
    if policy == "full":
        candidate._seq_lens_cpu = candidate.seq_lens.cpu()
    else:
        adapter = PagedHistoryAdapter(config, batch, "npu:0")
        adapter.begin_step(requests, list(requests))
        adapter.apply(candidate, 1, page)
    device_key, device_value = physical_key.npu(), physical_value.npu()
    actual, _ = torch_npu.npu_fused_infer_attention_score(
        query=query.npu(),
        key=device_key.view(batch * columns + 1, page, -1),
        value=device_value.view(batch * columns + 1, page, -1),
        block_table=candidate.block_table_tensor,
        input_layout="TND",
        block_size=page,
        actual_seq_lengths=[(row + 1) * query_tokens for row in range(batch)],
        actual_seq_lengths_kv=candidate._seq_lens_cpu.tolist(),
        num_key_value_heads=kv_heads,
        num_heads=query_heads,
        scale=head_dim**-0.5,
        sparse_mode=0,
    )
    expected, position_digests = [], []
    actual_table = candidate.block_table_tensor.cpu()
    for row, length in enumerate(lengths):
        start = max(0, length - window) // page * page
        positions = []
        if policy == "bm25":
            eligible = max(0, len(requests[str(row)].prompt_token_ids) - window) // page
            anchor = 3 if eligible > 3 else eligible - 1
            if 0 <= anchor < start // page:
                positions += list(range(anchor * page, (anchor + 1) * page))
        positions += list(range(start, length))
        inverse = {int(physical): logical for logical, physical in enumerate(physical_table[row])}
        observed = [
            inverse[int(actual_table[row, index // page])] * page + index % page
            for index in range(int(candidate._seq_lens_cpu[row]))
        ]
        if observed != positions:
            raise AssertionError("NPU compact table differs from the independent absolute-position oracle")
        selection = torch.tensor(positions, dtype=torch.int64)
        selected_key = logical_key[row, selection].float().repeat_interleave(query_heads // kv_heads, dim=1)
        selected_value = logical_value[row, selection].float().repeat_interleave(query_heads // kv_heads, dim=1)
        q = query[row * query_tokens : (row + 1) * query_tokens].float()
        scores = torch.einsum("qhd,khd->hqk", q, selected_key) * head_dim**-0.5
        expected.append(torch.einsum("hqk,khd->qhd", scores.softmax(dim=-1), selected_value))
        position_digests.append(hashlib.sha256(selection.numpy().tobytes()).hexdigest())
    expected = torch.cat(expected)
    observed = actual.cpu().float().reshape_as(expected)
    torch.testing.assert_close(observed, expected, atol=0.02, rtol=0.02)
    if not torch.equal(device_key.cpu(), physical_key) or not torch.equal(device_value.cpu(), physical_value):
        raise AssertionError("draft reads modified the retained full physical KV cache")
    if candidate.slot_mapping is not original.slot_mapping or candidate.positions is not original.positions:
        raise AssertionError("draft read adaptation replaced absolute write slots or positions")
    return {
        "policy": policy,
        "input_lengths_including_queries": list(lengths),
        "query_tokens": query_tokens,
        "actual_read_lengths": candidate._seq_lens_cpu.tolist(),
        "page_tokens": page,
        "query_heads": query_heads,
        "kv_heads": kv_heads,
        "head_dim": head_dim,
        "max_abs_error": float((observed - expected).abs().max()),
        "atol": 0.02,
        "rtol": 0.02,
        "position_sha256": position_digests,
        "full_cache_unchanged": True,
        "absolute_slots_and_positions_unchanged": True,
        "passed": True,
    }


def main(output, lengths=None, queries=None, selected_policy=None):
    torch.npu.set_device(0)
    report = {
        "scope": "synthetic real paged FIA numerical validation; not full-model or latency evidence",
        "device": torch.npu.get_device_name(0),
        "torch_npu": torch_npu.__version__,
        "source_sha256": {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in (Path(__file__).name, "paged_history.py", "history_policy.py")
        },
        "checks": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    cases = (tuple(lengths),) if lengths else ((2300,), (4099, 24583), (512, 16391, 24583))
    query_counts = (queries,) if queries else (7, 8)
    for lengths in cases:
        for queries in query_counts:
            for policy in (selected_policy,) if selected_policy else ("window", "bm25"):
                try:
                    record = check_case(lengths, queries, policy)
                except Exception as error:
                    report["checks"].append(
                        {
                            "lengths": list(lengths),
                            "query_tokens": queries,
                            "policy": policy,
                            "passed": False,
                            "error": str(error),
                        }
                    )
                    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                    raise
                report["checks"].append(record)
                output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "checks": len(report["checks"]),
                "all_passed": True,
                "max_abs_error": max(check["max_abs_error"] for check in report["checks"]),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lengths", type=int, nargs="+")
    parser.add_argument("--queries", type=int)
    parser.add_argument("--policy", choices=["full", "window", "bm25"])
    args = parser.parse_args()
    main(args.output, args.lengths, args.queries, args.policy)
