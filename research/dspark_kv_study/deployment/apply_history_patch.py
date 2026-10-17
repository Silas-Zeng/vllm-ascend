"""Install a minimal draft-only overlay in a verified, isolated package copy.

The source checksums identify the official 0.26 image and the supplied 910B2
image. Never apply this to the host's shared package. Actual builder regression
must pass before --apply is allowed to change the isolated file.
"""

import argparse
import ast
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path


def patched_source(source):
    allowed = (
        "e3e1ff579e67f845f155c983294ae5546e6ee76ba330098d7867e870c3f9f6e4",
        "25f0e85ea44291448b8a1b91408d6e4f8c80636646b603f78dd1c3a8fdece01f",
    )
    if hashlib.sha256(source.encode()).hexdigest() not in allowed:
        raise ValueError("inspect this runtime version before extending the source checksum allowlist")
    replacements = (
        (
            "from vllm_ascend.spec_decode.utils import (",
            "from paged_history import begin_dspark_history\n\nfrom vllm_ascend.spec_decode.utils import (",
        ),
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
            "        history_adapter = begin_dspark_history(self, common_attn_metadata) "
            'if self.method == "dspark" else None\n'
            "        for attn_group in self.draft_attn_groups:",
        ),
        (
            "                gid = attn_group.kv_cache_group_id\n"
            "                common_attn_metadata = copy.copy(common_attn_metadata)",
            "                gid = attn_group.kv_cache_group_id\n"
            "                common_attn_metadata = copy.copy(full_common_attn_metadata)",
        ),
        (
            "                attn_metadata = builder.build_for_drafting(\n"
            "                    common_attn_metadata, draft_index=1, **extra_attn_metadata_args\n"
            "                )",
            "                if history_adapter is not None:\n"
            "                    history_adapter.apply(\n"
            "                        common_attn_metadata, gid, self._per_group_kernel_block_sizes[gid]\n"
            "                    )\n"
            "                attn_metadata = builder.build_for_drafting(\n"
            "                    common_attn_metadata, draft_index=1, **extra_attn_metadata_args\n"
            "                )",
        ),
    )
    result = source
    for before, after in replacements:
        if result.count(before) != 1:
            raise ValueError("runtime source lacks the unique expected patch context")
        result = result.replace(before, after)
    ast.parse(result)
    return result


def main(args):
    from validate_paged_history import verify_actual_builder

    package = Path(importlib.util.find_spec("vllm_ascend").origin).parent.resolve()
    if not package.is_relative_to(args.isolated_root.resolve()):
        raise ValueError("the imported package is not inside the explicitly selected isolated runtime")
    source_path = package / "spec_decode/llm_base_proposer.py"
    original = source_path.read_text(encoding="utf-8")
    candidate = patched_source(original)
    checks = verify_actual_builder(candidate)
    if not all(check["passed"] for check in checks):
        raise AssertionError("actual patched builder failed the physical-position oracle")
    report = {
        "scope": "actual builder on CPU; full model and NPU regression are separate gates",
        "before_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "after_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
        "checks": checks,
        "applied": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".patch").write_text(
        "".join(
            difflib.unified_diff(
                original.splitlines(True),
                candidate.splitlines(True),
                fromfile="original/llm_base_proposer.py",
                tofile="isolated/llm_base_proposer.py",
            )
        ),
        encoding="utf-8",
    )
    if args.apply:
        backup = source_path.with_suffix(".py.history_study_original")
        if backup.exists():
            raise ValueError("an overlay backup already exists; inspect it before changing source")
        backup.write_text(original, encoding="utf-8")
        source_path.write_text(candidate, encoding="utf-8")
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != report["after_sha256"]:
            raise AssertionError("installed overlay differs from the validated candidate")
        report["applied"] = True
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args())
