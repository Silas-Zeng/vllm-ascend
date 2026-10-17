"""Backport the known DSpark window apply-point fix to the pinned 0.26 image.

The original image file must match the recorded SHA-256. Before modifying it,
execute its actual builder method on CPU and compare physical read positions
with an independent absolute-position oracle. This is not a model quality test.
"""

import argparse
import ast
import copy
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def patched_source(source):
    replacements = (
        (
            'future_offset = 0 if self.method == "dflash" else self.num_speculative_tokens',
            'future_offset = 0 if self.method in ("dflash", "dspark") else self.num_speculative_tokens',
        ),
        (
            "        if self.draft_window_size is not None:\n"
            "            self.sliding_window.apply(common_attn_metadata)\n",
            '        if self.draft_window_size is not None and self.method != "dspark":\n'
            "            self.sliding_window.apply(common_attn_metadata)\n",
        ),
        (
            "                attn_metadata = builder.build_for_drafting(\n"
            "                    common_attn_metadata, draft_index=1, **extra_attn_metadata_args\n"
            "                )",
            "                # DSpark overwrites the per-group table above; window its final read metadata.\n"
            "                if self.draft_window_size is not None:\n"
            "                    self.sliding_window.apply(common_attn_metadata)\n"
            "                attn_metadata = builder.build_for_drafting(\n"
            "                    common_attn_metadata, draft_index=1, **extra_attn_metadata_args\n"
            "                )",
        ),
    )
    result = source
    for before, after in replacements:
        if result.count(before) != 1:
            raise ValueError("runtime source does not contain the unique expected patch context")
        result = result.replace(before, after)
    ast.parse(result)
    return result


def definition(source, name, namespace):
    matches = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name
    ]
    if len(matches) != 1:
        raise ValueError("ambiguous source definition")
    module = ast.Module(body=[matches[0]], type_ignores=[])
    exec(compile(module, "<pinned-runtime-definition>", "exec"), namespace)
    return namespace[name]


def verify_builder(source, adapter_source, pre_apply):
    import torch

    adapter_type = definition(adapter_source, "SlidingWindowAdapter", {"torch": torch})
    builder_method = definition(source, "build_draft_attn_metadata", {"copy": copy, "Any": object})
    records = []
    for lengths in ((256,), (636,), (1543,), (2300,), (24583,), (256, 1543), (1543, 24583)):
        block_size, window = 128, 2176
        count = len(lengths)
        columns = (max(lengths) + block_size - 1) // block_size
        full_table = torch.arange(count * columns, dtype=torch.int32).reshape(count, columns) + 100
        seq_lens = torch.tensor(lengths, dtype=torch.int32)
        slot_mapping = torch.arange(count * 7, dtype=torch.int32) + 1000
        captured = {}

        def capture(metadata, captured=captured, **kwargs):
            captured["table"] = metadata.block_table_tensor.clone()
            captured["lengths"] = metadata.seq_lens.clone()
            captured["cpu_lengths"] = metadata.seq_lens_cpu.clone()
            captured["slots"] = metadata.slot_mapping.clone()
            return SimpleNamespace(causal=True)

        builder = SimpleNamespace(build_for_drafting=capture)
        group = SimpleNamespace(kv_cache_group_id=1, layer_names=["draft"], get_metadata_builder=lambda b=builder: b)
        adapter = adapter_type(window, block_size, count, 7 if pre_apply else 0, torch.device("cpu"))
        proposer = SimpleNamespace(
            draft_attn_groups=[group],
            use_compress=False,
            method="dspark",
            draft_window_size=window,
            sliding_window=adapter,
            _per_group_block_table_buffers={1: full_table},
            _per_group_query_slot_mapping_buffers={1: slot_mapping},
        )
        common = SimpleNamespace(
            seq_lens=seq_lens.clone(),
            seq_lens_cpu=seq_lens.clone(),
            num_reqs=count,
            block_table_tensor=full_table,
            slot_mapping=slot_mapping,
        )
        if pre_apply:
            adapter.apply(common)
        builder_method(proposer, common, count * 7, count * 7)
        valid = torch.equal(captured["lengths"], captured["cpu_lengths"]) and torch.equal(
            captured["slots"], slot_mapping
        )
        for row, length in enumerate(lengths):
            expected_start = max(0, length - window) // block_size * block_size
            reverse = {int(physical): logical for logical, physical in enumerate(full_table[row])}
            observed = [
                reverse[int(captured["table"][row, offset // block_size])] * block_size + offset % block_size
                for offset in range(int(captured["lengths"][row]))
            ]
            valid = valid and observed == list(range(expected_start, length))
        if not pre_apply:
            valid = (
                valid and torch.equal(common.seq_lens, seq_lens) and torch.equal(common.block_table_tensor, full_table)
            )
        records.append({"lengths": list(lengths), "physical_read_positions_correct": bool(valid)})
    return records


def main(args):
    package = Path(importlib.util.find_spec("vllm_ascend").origin).parent
    source_path = package / "spec_decode/llm_base_proposer.py"
    original = source_path.read_text(encoding="utf-8")
    checksum = hashlib.sha256(original.encode()).hexdigest()
    if checksum != "e3e1ff579e67f845f155c983294ae5546e6ee76ba330098d7867e870c3f9f6e4":
        raise ValueError("runtime base proposer differs from the probed official image")
    candidate = patched_source(original)
    adapter_source = (package / "spec_decode/utils.py").read_text(encoding="utf-8")
    before = verify_builder(original, adapter_source, pre_apply=True)
    after = verify_builder(candidate, adapter_source, pre_apply=False)
    if all(record["physical_read_positions_correct"] for record in before):
        raise AssertionError("the expected regression was not reproduced")
    if not all(record["physical_read_positions_correct"] for record in after):
        raise AssertionError("patched builder failed the absolute-position oracle")
    report = {
        "scope": "CPU metadata wiring regression; not full model validation",
        "before_sha256": checksum,
        "after_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
        "before": before,
        "after": after,
        "applied": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(True),
            candidate.splitlines(True),
            fromfile="original/llm_base_proposer.py",
            tofile="patched/llm_base_proposer.py",
        )
    )
    args.output.with_suffix(".patch").write_text(diff, encoding="utf-8")
    if args.apply:
        backup = source_path.with_suffix(".py.dspark_study_original")
        if backup.exists():
            raise ValueError("an earlier runtime overlay already exists; inspect it before replacing")
        backup.write_text(original, encoding="utf-8")
        source_path.write_text(candidate, encoding="utf-8")
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != report["after_sha256"]:
            raise AssertionError("installed overlay does not match validated source")
        report["applied"] = True
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args())
