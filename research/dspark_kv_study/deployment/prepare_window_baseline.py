"""Create a separate, checksum-pinned DSpark window baseline package.

The stock builder restores the full per-group table after cropping lengths.
Window each group's final read metadata, using independent adapter buffers.
AR and Full KV continue importing the untouched native package.
"""

import argparse
import ast
import copy
import difflib
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from apply_window_patch import definition

SUPPORTED_SOURCE_SHA256 = "25f0e85ea44291448b8a1b91408d6e4f8c80636646b603f78dd1c3a8fdece01f"
WINDOW_SIZE = 2176


def patch_source(source):
    replacements = [
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
            "        per_layer_attn_metadata: dict[str, Any] = {}\n        for attn_group in self.draft_attn_groups:",
            "        per_layer_attn_metadata: dict[str, Any] = {}\n"
            "        full_common_attn_metadata = common_attn_metadata\n"
            "        for attn_group in self.draft_attn_groups:",
        ),
        (
            "                common_attn_metadata = copy.copy(common_attn_metadata)\n"
            '                block_table = getattr(self, "_per_group_block_table_buffers", {}).get(gid)',
            "                common_attn_metadata = copy.copy(full_common_attn_metadata)\n"
            '                block_table = getattr(self, "_per_group_block_table_buffers", {}).get(gid)',
        ),
        (
            "                attn_metadata = builder.build_for_drafting(\n"
            "                    common_attn_metadata, draft_index=1, **extra_attn_metadata_args\n"
            "                )",
            "                # study_window_baseline_per_group: crop final reads; preserve absolute writes.\n"
            "                if self.draft_window_size is not None:\n"
            '                    if not hasattr(self, "_study_group_windows"):\n'
            "                        self._study_group_windows = {}\n"
            "                    if gid not in self._study_group_windows:\n"
            "                        self._study_group_windows[gid] = SlidingWindowAdapter(\n"
            "                            self.draft_window_size, self._per_group_kernel_block_sizes[gid],\n"
            "                            self.runner.max_num_reqs, 0, self.device,\n"
            "                        )\n"
            "                    self._study_group_windows[gid].apply(common_attn_metadata)\n"
            "                attn_metadata = builder.build_for_drafting(\n"
            "                    common_attn_metadata, draft_index=1, **extra_attn_metadata_args\n"
            "                )",
        ),
    ]
    result = source
    for original, replacement in replacements:
        if result.count(original) != 1:
            raise ValueError("pinned window patch context is missing or ambiguous")
        result = result.replace(original, replacement)
    ast.parse(result)
    return result


def verify_multigroup_builder(source, adapter_source, stock):
    import torch

    adapter_type = definition(adapter_source, "SlidingWindowAdapter", {"torch": torch})
    method = definition(
        source, "build_draft_attn_metadata", {"copy": copy, "Any": object, "SlidingWindowAdapter": adapter_type}
    )
    cases = [(value,) for value in (256, 2175, 2176, 2177, 2303, 2304, 4096, 16384, 24583)]
    cases += [(256, 24583), (4096, 16384, 24583, 2304)]
    records = []
    for lengths in cases:
        for block_sizes in [(128, 128), (64, 128)]:
            count = len(lengths)
            full_tables, slots, groups, captures = {}, {}, [], {}
            for gid, block_size in enumerate(block_sizes):
                columns = (max(lengths) + block_size - 1) // block_size
                table = torch.arange(count * columns, dtype=torch.int32).reshape(count, columns) + 100 + gid * 10000
                full_tables[gid] = table
                slots[gid] = torch.tensor(
                    [
                        int(table[row, position // block_size]) * block_size + position % block_size
                        for row, length in enumerate(lengths)
                        for position in range(length - 7, length)
                    ],
                    dtype=torch.int32,
                )

                def capture(metadata, gid=gid, captures=captures, **kwargs):
                    captures[gid] = metadata
                    return SimpleNamespace(causal=True)

                builder = SimpleNamespace(build_for_drafting=capture)
                groups.append(
                    SimpleNamespace(
                        kv_cache_group_id=gid,
                        layer_names=[f"draft.{gid}"],
                        get_metadata_builder=lambda builder=builder: builder,
                    )
                )
            seq = torch.tensor(lengths, dtype=torch.int32)
            common = SimpleNamespace(
                seq_lens=seq.clone(),
                seq_lens_cpu=seq.clone(),
                _seq_lens_cpu=seq.clone(),
                parallel_draft_seq_lens_cpu=seq.clone(),
                seq_lens_cpu_upper_bound=seq.clone(),
                num_reqs=count,
                block_table_tensor=full_tables[0],
                slot_mapping=slots[0],
            )
            proposer = SimpleNamespace(
                draft_attn_groups=groups,
                use_compress=False,
                method="dspark",
                draft_window_size=WINDOW_SIZE,
                sliding_window=adapter_type(WINDOW_SIZE, block_sizes[0], count, 7, torch.device("cpu")),
                _per_group_block_table_buffers=full_tables,
                _per_group_query_slot_mapping_buffers=slots,
                _per_group_kernel_block_sizes=dict(enumerate(block_sizes)),
                runner=SimpleNamespace(max_num_reqs=count),
                device=torch.device("cpu"),
            )
            if stock:
                proposer.sliding_window.apply(common)
            method(proposer, common, count * 7, count * 7)
            for gid, block_size in enumerate(block_sizes):
                metadata = captures[gid]
                passed = torch.equal(metadata.slot_mapping, slots[gid])
                for row, length in enumerate(lengths):
                    start = max(0, length - WINDOW_SIZE) // block_size * block_size
                    actual_length = int(metadata.seq_lens[row])
                    passed &= actual_length == length - start
                    required_blocks = (actual_length + block_size - 1) // block_size
                    expected = full_tables[gid][row, start // block_size : start // block_size + required_blocks]
                    passed &= torch.equal(metadata.block_table_tensor[row, :required_blocks], expected)
                    for attr in [
                        "seq_lens_cpu",
                        "_seq_lens_cpu",
                        "parallel_draft_seq_lens_cpu",
                        "seq_lens_cpu_upper_bound",
                    ]:
                        passed &= torch.equal(getattr(metadata, attr), metadata.seq_lens)
                if not stock:
                    passed &= torch.equal(common.seq_lens, seq)
                    passed &= torch.equal(common.block_table_tensor, full_tables[0])
                records.append(
                    {"lengths": list(lengths), "block_sizes": list(block_sizes), "gid": gid, "passed": bool(passed)}
                )
    return records


def main(args):
    source_root = args.native_package.resolve()
    target_root = args.isolated_root.resolve()
    if not target_root.is_relative_to(args.study_root.resolve()) or target_root == source_root.parent:
        raise ValueError("window runtime must be a separate directory inside this study")
    source_file = source_root / "spec_decode/llm_base_proposer.py"
    original = source_file.read_text(encoding="utf-8")
    digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
    if digest != SUPPORTED_SOURCE_SHA256:
        raise ValueError("native proposer checksum differs from the verified 910B2 source")
    candidate = patch_source(original)
    adapter = (source_root / "spec_decode/utils.py").read_text(encoding="utf-8")
    before = verify_multigroup_builder(original, adapter, True)
    after = verify_multigroup_builder(candidate, adapter, False)
    if all(row["passed"] for row in before) or not all(row["passed"] for row in after):
        raise ValueError("window regression or candidate physical-position oracle failed")
    if target_root.exists():
        raise ValueError("window runtime already exists; inspect it instead of replacing frozen sources")
    target_root.mkdir(parents=True)
    shutil.copytree(source_root, target_root / "vllm_ascend", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (target_root / "vllm_ascend/spec_decode/llm_base_proposer.py").write_text(candidate, encoding="utf-8")
    report = {
        "scope": "CPU actual-builder regression; live NPU read audit and output equality still required",
        "native_package": str(source_root),
        "window_runtime": str(target_root),
        "before_sha256": digest,
        "after_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
        "before": before,
        "after": after,
        "changed_native_files": 0,
        "changed_window_package_files": ["spec_decode/llm_base_proposer.py"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".patch").write_text(
        "".join(
            difflib.unified_diff(
                original.splitlines(True), candidate.splitlines(True), fromfile="native", tofile="window_baseline"
            )
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "before_failed": sum(not r["passed"] for r in before),
                "after_passed": len(after),
                "after_sha256": report["after_sha256"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-package", type=Path, required=True)
    parser.add_argument("--isolated-root", type=Path, required=True)
    parser.add_argument("--study-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
