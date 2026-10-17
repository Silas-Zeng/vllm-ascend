"""Create exact-token-length, controlled retrieval inputs using the real tokenizer.

These are synthetic tasks, not a general quality benchmark. Expected answers
are evaluation metadata only; generation and retrieval receive prompt/query IDs.
"""

import argparse
import hashlib
import json
from pathlib import Path

from history_policy import BM25HistoryIndex, select_positions


def make_request(tokenizer, context, request_index, long_form=False):
    entities = ("Polaris", "Meridian", "Juniper", "Helios")
    placements = ("far", "middle", "recent", "far")
    entity = entities[request_index % len(entities)]
    answer = str(73129 + 137 * request_index)
    question = f"What is the recovery code for project {entity}? Reply with the code only."
    if long_form:
        question = (
            f"What is the recovery code for project {entity}? Start with that code. "
            "Then write a detailed recovery checklist for a distributed storage service, with at least twenty numbered "
            "steps. For every step provide several complete sentences covering verification, "
            "audit logging, and rollback. "
            "Explain why each check is necessary and continue until the whole checklist is complete."
        )
    marker = "__STUDY_PAYLOAD_BOUNDARY__"
    template = tokenizer.apply_chat_template(
        [{"role": "user", "content": marker}], tokenize=False, add_generation_prompt=True, enable_thinking=False
    )
    if template.count(marker) != 1:
        raise ValueError("tokenizer chat template does not preserve the payload boundary")
    opening, closing = template.split(marker)
    prefix = tokenizer.encode(opening, add_special_tokens=False)
    suffix = tokenizer.encode(closing, add_special_tokens=False)
    query_ids = tokenizer.encode(question, add_special_tokens=False)
    question_ids = tokenizer.encode("\n\nQuestion: " + question + "\n", add_special_tokens=False)
    fact = tokenizer.encode(
        f"\nThe recovery code for project {entity} is {answer}. This is the authoritative recovery record.\n",
        add_special_tokens=False,
    )
    filler = tokenizer.encode(
        "The engineering archive records cache allocation, request scheduling, and document maintenance. "
        "Each section reviews ordinary procedures and reports no changes to the audit schedule. "
        "Storage pages are tracked by their original position and verified before use.\n",
        add_special_tokens=False,
    )
    history_length = context - len(prefix) - len(suffix) - len(question_ids)
    if history_length < 512 or len(fact) > 96:
        raise ValueError("context is too short or fact no longer fits a retrieval chunk")
    history = (filler * (history_length // len(filler) + 1))[:history_length]
    placement = placements[request_index % len(placements)]
    if placement == "middle":
        absolute_start = ((len(prefix) + history_length // 2) // 128) * 128 + 16
    elif placement == "recent":
        absolute_start = ((len(prefix) + history_length - 384) // 128) * 128 + 16
    else:
        absolute_start = 272
    offset = absolute_start - len(prefix)
    if offset < 0 or offset + len(fact) > history_length:
        raise ValueError("fact falls outside the requested history")
    history[offset : offset + len(fact)] = fact
    prompt_ids = prefix + history + question_ids + suffix
    if len(prompt_ids) != context:
        raise AssertionError("token-length contract failed")
    selection = select_positions(BM25HistoryIndex(prompt_ids), query_ids, context)
    fact_positions = set(range(absolute_start, absolute_start + len(fact)))
    return {
        "request_id": f"c{context}-request-{request_index}",
        "prompt_token_ids": prompt_ids,
        "query_token_ids": query_ids,
        "question": question,
        "expected_answer": answer,
        "fact_placement": placement,
        "fact_token_span": [absolute_start, absolute_start + len(fact)],
        "retrieved_anchor_blocks": list(selection.anchor_blocks),
        "selection_contains_fact": fact_positions.issubset(selection.positions),
    }


def main(args):
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True, trust_remote_code=False)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "scope": "controlled_synthetic_retrieval_not_general_model_quality",
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "tokenizer_config_sha256": hashlib.sha256((args.tokenizer / "tokenizer_config.json").read_bytes()).hexdigest(),
        "enable_thinking": False,
        "long_form": args.long_form,
        "files": [],
    }
    for context in args.contexts:
        for batch in args.batches:
            requests = [make_request(tokenizer, context, index, args.long_form) for index in range(batch)]
            task_prefix = "prompts_long" if args.long_form else "prompts"
            path = args.output / f"{task_prefix}_c{context}_b{batch}.json"
            path.write_text(json.dumps(requests, indent=2) + "\n", encoding="utf-8")
            record = {
                "name": path.name,
                "context_tokens": context,
                "batch": batch,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "anchor_blocks": [request["retrieved_anchor_blocks"] for request in requests],
                "retrieval_hits": [request["selection_contains_fact"] for request in requests],
            }
            manifest["files"].append(record)
            print(json.dumps(record), flush=True)
    (args.output / "prompt_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--contexts", type=int, nargs="+", default=[4096, 16384, 24576])
    parser.add_argument("--batches", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--long-form", action="store_true", help="request a long natural continuation for 512-token timing"
    )
    main(parser.parse_args())
