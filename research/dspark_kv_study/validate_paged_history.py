"""Execute the actual patched builder against an absolute-page CPU oracle."""

import copy
from types import SimpleNamespace

import torch
from deployment.apply_window_patch import definition
from paged_history import begin_dspark_history


def verify_actual_builder(source):
    builder_method = definition(
        source, "build_draft_attn_metadata", {"copy": copy, "Any": object, "begin_dspark_history": begin_dspark_history}
    )
    checks = []
    for policy in ("window", "bm25"):
        for lengths in ((256,), (2300,), (4099,), (24583,), (256, 24583)):
            page, batch = 128, len(lengths)
            columns = (max(lengths) + page - 1) // page
            tables, slots, captures, requests, groups = {}, {}, {}, {}, []
            for row, length in enumerate(lengths):
                prompt = [0] * (length - 7)
                if len(prompt) >= 512:
                    prompt[384:512] = [999] * 128
                requests[str(row)] = SimpleNamespace(
                    prompt_token_ids=prompt,
                    sampling_params=SimpleNamespace(extra_args={"history_query_token_ids": [999]}),
                )
            for group_id in (1, 2):
                tables[group_id] = (
                    torch.arange(batch * columns, dtype=torch.int32).reshape(batch, columns).flip(1) + group_id * 1000
                )
                slots[group_id] = torch.arange(batch * 7, dtype=torch.int32) + group_id * 10000

                def capture(metadata, group_id=group_id, captures=captures, **kwargs):
                    captures[group_id] = copy.copy(metadata)
                    return SimpleNamespace(causal=False)

                builder = SimpleNamespace(build_for_drafting=capture)
                groups.append(
                    SimpleNamespace(
                        kv_cache_group_id=group_id,
                        layer_names=[f"draft-{group_id}"],
                        get_metadata_builder=lambda builder=builder: builder,
                    )
                )
            config = {} if policy == "window" else {"research_history_kv": {"policy": "bm25"}}
            proposer = SimpleNamespace(
                vllm_config=SimpleNamespace(additional_config=config),
                method="dspark",
                use_compress=False,
                use_cuda_graph=False,
                dcp_size=1,
                draft_window_size=2176 if policy == "window" else None,
                runner=SimpleNamespace(
                    max_num_reqs=batch, requests=requests, input_batch=SimpleNamespace(req_ids=list(requests))
                ),
                device=torch.device("cpu"),
                draft_attn_groups=groups,
                _per_group_block_table_buffers=tables,
                _per_group_query_slot_mapping_buffers=slots,
                _per_group_kernel_block_sizes={1: page, 2: page},
            )
            exact = torch.tensor(lengths, dtype=torch.int32)
            common = SimpleNamespace(
                num_reqs=batch,
                seq_lens=exact.clone(),
                _seq_lens_cpu=exact + 7,
                seq_lens_cpu=exact + 7,
                seq_lens_cpu_upper_bound=exact + 7,
                parallel_draft_seq_lens_cpu=exact + 14,
                max_seq_len=max(lengths),
                block_table_tensor=tables[1],
                slot_mapping=slots[1],
                positions=torch.arange(batch * 7, dtype=torch.int64) + 20000,
                causal=False,
            )
            builder_method(proposer, common, batch * 7, batch * 7)
            valid = torch.equal(common.seq_lens, exact) and common.block_table_tensor is tables[1]
            valid = valid and torch.equal(common._seq_lens_cpu, exact + 7)
            for group_id, captured in captures.items():
                valid = valid and captured.slot_mapping.data_ptr() == slots[group_id].data_ptr()
                valid = valid and captured.positions is common.positions
                valid = valid and torch.equal(captured.seq_lens, captured._seq_lens_cpu)
                for row, length in enumerate(lengths):
                    local = 2176 if policy == "window" else 2048
                    start = max(0, length - local) // page * page
                    expected = []
                    if policy == "bm25":
                        eligible = max(0, len(requests[str(row)].prompt_token_ids) - local) // page
                        anchor = 3 if eligible > 3 else eligible - 1
                        if 0 <= anchor < start // page:
                            expected += list(range(anchor * page, (anchor + 1) * page))
                    expected += list(range(start, length))
                    reverse = {int(physical): logical for logical, physical in enumerate(tables[group_id][row])}
                    observed = [
                        reverse[int(captured.block_table_tensor[row, position // page])] * page + position % page
                        for position in range(int(captured.seq_lens[row]))
                    ]
                    valid = valid and observed == expected
            valid = valid and captures[1].block_table_tensor.data_ptr() != captures[2].block_table_tensor.data_ptr()
            checks.append({"policy": policy, "lengths": list(lengths), "kv_groups": 2, "passed": bool(valid)})
    return checks
