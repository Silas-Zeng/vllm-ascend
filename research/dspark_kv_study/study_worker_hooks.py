"""Read-only diagnostics and profiler annotations, enabled only in separate runs."""

import functools
import json
import os
from pathlib import Path


class StudyWorkerExtension:
    def study_inspect(self):
        return inspect_worker(self)

    def study_install_observers(self, audit_directory=None, annotate=False):
        return install_worker_observers(self, audit_directory, annotate)


def target_phase(metadata, speculative):
    if metadata is None:
        return "unknown"
    prefills = getattr(metadata, "num_prefills", None)
    decodes = getattr(metadata, "num_decodes", None)
    if prefills is None or decodes is None:
        return "unknown"
    if prefills:
        return "mixed" if decodes else "prefill"
    return "verify" if speculative else "decode"


def target_execution(context, wrapper):
    """Mirror ACLGraphWrapper's branch, including unexpected lazy captures."""
    mode = getattr(context, "cudagraph_runtime_mode", None)
    if mode is None:
        return "unknown"
    if getattr(mode, "name", None) == "NONE" or mode != wrapper.runtime_mode:
        return "eager"
    entry = wrapper.concrete_aclgraph_entries.get(context.batch_descriptor)
    return "graph" if entry is not None and entry.aclgraph is not None else "capture"


def inspect_worker(worker):
    import torch

    runner = worker.model_runner
    drafter = getattr(runner, "drafter", None)
    cache = getattr(runner, "kv_cache_config", None)
    return {
        "pid": os.getpid(),
        "runner_class": type(runner).__name__,
        "target_wrapper": type(runner.model).__name__,
        "drafter_class": type(drafter).__name__ if drafter else None,
        "draft_model_class": type(drafter.model).__name__ if drafter else None,
        "window_size": getattr(drafter, "draft_window_size", None),
        "num_query_per_request": getattr(drafter, "num_query_per_req", None),
        "sample_from_anchor": getattr(drafter, "sample_from_anchor", None),
        "kernel_block_sizes": getattr(drafter, "_per_group_kernel_block_sizes", {}),
        "draft_groups": [
            {"gid": group.kv_cache_group_id, "layers": list(group.layer_names)}
            for group in getattr(drafter, "draft_attn_groups", [])
        ],
        "kv_cache_num_blocks": getattr(cache, "num_blocks", None),
        "kv_cache_group_count": len(getattr(cache, "kv_cache_groups", [])),
        "npu_allocated_bytes": torch.npu.memory_allocated(),
        "npu_reserved_bytes": torch.npu.memory_reserved(),
        "npu_peak_allocated_bytes": torch.npu.max_memory_allocated(),
    }


def _annotate_method(instance, name, label, phase_provider=None):
    import torch

    original = getattr(instance, name)

    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        scoped_label = label + "/" + phase_provider() if phase_provider else label
        with torch.profiler.record_function(scoped_label):
            return original(*args, **kwargs)

    setattr(instance, name, wrapped)


def install_worker_observers(worker, audit_directory=None, annotate=False):
    """Install separate read audits or profiler scopes without modifying model math.

    Audits deliberately copy metadata to the CPU and are excluded from timings.
    Profiler labels follow attention metadata paths and the graph wrapper branch.
    """
    import torch
    from vllm.config import get_layers_from_vllm_config
    from vllm.model_executor.layers.attention_layer_base import AttentionLayerBase

    runner = worker.model_runner
    drafter = getattr(runner, "drafter", None)
    if audit_directory:
        if drafter is None:
            raise ValueError("draft-read auditing requires a speculative model")
        from vllm_ascend.attention.attention_v1 import AscendAttentionBackend

        impl_class = AscendAttentionBackend.get_impl_cls()
        original_params = impl_class._get_fia_params
        layers = get_layers_from_vllm_config(worker.vllm_config, AttentionLayerBase)
        draft_impls = {
            id(layers[name].impl): (group.kv_cache_group_id, name)
            for group in drafter.draft_attn_groups
            for name in group.layer_names
        }
        output = Path(audit_directory) / f"worker_{os.getpid()}_draft_reads.jsonl"
        output.parent.mkdir(parents=True, exist_ok=True)

        @functools.wraps(original_params)
        def audited_params(impl, *args, **kwargs):
            result = original_params(impl, *args, **kwargs)
            if id(impl) not in draft_impls:
                return result
            gid, name = draft_impls[id(impl)]
            _, _, block_size, table, lengths = result
            metadata = args[2] if len(args) > 2 else kwargs["attn_metadata"]
            if table is None:
                return result
            if isinstance(lengths, torch.Tensor):
                lengths = lengths.detach().cpu().tolist()
            req_ids = list(runner.input_batch.req_ids)
            n = min(len(req_ids), len(lengths))
            full = drafter._per_group_block_tables[gid][:n].detach().cpu().tolist()
            read = table[:n].detach().cpu().tolist()
            positions = drafter.positions[: n * drafter.num_query_per_req].detach().cpu().tolist()
            slots = metadata.slot_mapping[: n * drafter.num_query_per_req].detach().cpu().tolist()
            record = {
                "gid": gid,
                "layer": name,
                "request_ids": req_ids[:n],
                "block_size": block_size,
                "window_config": drafter.draft_window_size,
                "kv_lengths": list(lengths[:n]),
                "read_block_tables": read,
                "full_block_tables": full,
                "query_positions": positions,
                "query_slot_mapping": slots,
                "query_per_request": drafter.num_query_per_req,
            }
            with output.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")
            return result

        impl_class._get_fia_params = audited_params
    if annotate:
        worker._study_target_phase = "unknown"

        def current_target_phase():
            return worker._study_target_phase

        def current_draft_phase():
            phase = worker._study_target_phase
            return "decode" if phase in ("decode", "verify") else phase

        _annotate_method(worker, "execute_model", "study/worker_execute")
        target = runner.model
        target_class = type(target)
        original_call = target_class.__call__

        @functools.wraps(original_call)
        def target_call(instance, *args, **kwargs):
            if instance is target:
                from vllm.forward_context import get_forward_context

                try:
                    context = get_forward_context()
                    context_metadata = context.attn_metadata
                except (AssertionError, RuntimeError):
                    context = None
                    context_metadata = None
                metadata = next(iter(context_metadata.values()), None) if isinstance(context_metadata, dict) else None
                phase = target_phase(metadata, drafter is not None)
                worker._study_target_phase = phase
                with (
                    torch.profiler.record_function("study/target_forward"),
                    torch.profiler.record_function(f"study/target_{phase}"),
                    torch.profiler.record_function(f"study/target_{target_execution(context, instance)}/{phase}"),
                ):
                    return original_call(instance, *args, **kwargs)
            return original_call(instance, *args, **kwargs)

        target_class.__call__ = target_call
        target_model = runner.get_model()
        if hasattr(target_model, "compute_logits"):
            _annotate_method(target_model, "compute_logits", "study/target_logits", current_target_phase)
        for name, label in [("sampler", "study/target_sampler"), ("rejection_sampler", "study/rejection_sampler")]:
            sampler = getattr(runner, name, None)
            if sampler is not None and hasattr(sampler, "forward"):
                _annotate_method(sampler, "forward", label, current_target_phase)
        if drafter is not None:
            _annotate_method(drafter, "_propose", "study/draft_total", current_draft_phase)
            _annotate_method(drafter, "build_draft_attn_metadata", "study/draft_metadata", current_draft_phase)
            _annotate_method(drafter, "set_inputs_first_pass", "study/draft_inputs", current_draft_phase)
            _annotate_method(drafter, "_runnable", "study/draft_generate", current_draft_phase)
            _annotate_method(drafter, "build_model_inputs_first_pass", "study/draft_context_cache", current_draft_phase)
            _annotate_method(drafter.model, "forward", "study/draft_backbone", current_draft_phase)
            for method, label in [
                ("compute_logits", "study/draft_logits"),
                ("markov_embed", "study/markov_embed"),
                ("markov_bias", "study/markov_bias"),
            ]:
                _annotate_method(drafter.model, method, label, current_draft_phase)
            for group in drafter.draft_attn_groups:
                for name in group.layer_names:
                    layer = get_layers_from_vllm_config(worker.vllm_config, AttentionLayerBase)[name]
                    _annotate_method(layer.impl, "forward", "study/draft_attention", current_draft_phase)
    return {"audit_enabled": bool(audit_directory), "annotations_enabled": annotate, "pid": os.getpid()}
