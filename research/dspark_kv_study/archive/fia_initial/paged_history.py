"""Experimental DSpark draft read adapter; full KV storage remains intact.

Historical pages are ranked once from the immutable prompt and user question.
The recent window advances on every step. Attention sees complete historical
pages followed by recent pages and a possibly partial final page. RoPE positions
and KV write slots are never renumbered. This is an eager draft path, not a
claim of graph replay support or reduced allocated KV memory.
"""

from dataclasses import dataclass

import torch
from history_policy import BM25HistoryIndex


@dataclass(frozen=True)
class HistoryConfig:
    local_tokens: int = 2048
    anchor_tokens: int = 128
    policy: str = "bm25"
    cache_anchors: bool = True

    def __post_init__(self):
        if self.local_tokens <= 0 or self.anchor_tokens < 0:
            raise ValueError("invalid historical KV budget")
        if self.policy not in ("bm25", "uniform", "sink", "recent"):
            raise ValueError("unsupported historical selection policy")


def choose_anchor_pages(prompt_ids, query_ids, block_size, config):
    """Freeze complete pages older than the prompt's initial local window."""
    if block_size <= 0 or config.local_tokens % block_size or config.anchor_tokens % block_size:
        raise ValueError("budgets must contain complete attention pages")
    eligible = max(0, len(prompt_ids) - config.local_tokens) // block_size
    count = min(config.anchor_tokens // block_size, eligible)
    if count == 0:
        return ()
    if config.policy == "bm25":
        if not query_ids:
            raise ValueError("BM25 history requires the user question token IDs")
        index = BM25HistoryIndex(prompt_ids, block_tokens=block_size)
        selected = index.rank(query_ids, range(eligible))[:count]
    elif config.policy == "uniform":
        selected = tuple((2 * index + 1) * eligible // (2 * count) for index in range(count))
    elif config.policy == "sink":
        selected = tuple(range(count))
    else:
        selected = tuple(range(eligible - count, eligible))
    return tuple(sorted(selected))


class PagedHistoryAdapter:
    """Own request selections and independent, persistent per-group read tables."""

    def __init__(self, config, max_num_reqs, device):
        self.config = config
        self.max_num_reqs = max_num_reqs
        self.device = torch.device(device)
        self.entries = {}
        self.tables = {}
        self.anchor_batches = {}
        self.plans = {}
        self.requests = {}
        self.request_ids = ()
        self.selection_builds = 0
        self.anchor_batch_uploads = 0
        self.cpu_length_copies = 0

    def begin_step(self, requests, request_ids):
        self.requests = requests
        self.request_ids = tuple(request_ids)
        self.plans.clear()
        # Pending and active requests remain in runner.requests; retired IDs do not.
        self.entries = {key: value for key, value in self.entries.items() if key[0] in requests}

    def _anchors(self, block_size, num_reqs):
        count = self.config.anchor_tokens // block_size
        rows, identities = [], []
        for row in range(num_reqs):
            request_id = self.request_ids[row] if row < len(self.request_ids) else None
            state = self.requests.get(request_id)
            if state is None:
                rows.append((-1,) * count)
                identities.append(None)
                continue
            prompt = state.prompt_token_ids
            if prompt is None:
                raise ValueError("this research adapter supports text token prompts")
            sampling = state.sampling_params
            extra = getattr(sampling, "extra_args", None) or {}
            query = tuple(extra.get("history_query_token_ids", ()))
            if any(not isinstance(token, int) or token < 0 for token in query):
                raise ValueError("invalid history question token IDs")
            identity = (id(state), id(prompt), len(prompt), query)
            key = (request_id, block_size)
            cached = self.entries.get(key)
            if not self.config.cache_anchors or cached is None or cached[0] != identity:
                anchors = choose_anchor_pages(prompt, query, block_size, self.config)
                cached = (identity, anchors)
                self.selection_builds += 1
                if self.config.cache_anchors:
                    self.entries[key] = cached
            anchors = cached[1]
            rows.append(anchors + (-1,) * (count - len(anchors)))
            identities.append((request_id, identity, anchors))
        batch_key = tuple(identities)
        previous = self.anchor_batches.get(block_size)
        if previous is None or previous[0] != batch_key or not self.config.cache_anchors:
            tensor = torch.tensor(rows, dtype=torch.int64, device=self.device).reshape(num_reqs, count)
            previous = (batch_key, tensor)
            self.anchor_batches[block_size] = previous
            self.anchor_batch_uploads += 1
        return previous[1]

    def apply(self, metadata, group_id, block_size):
        """Mutate only a shallow draft metadata copy, after full group selection."""
        if self.config.local_tokens % block_size or self.config.anchor_tokens % block_size:
            raise ValueError("budgets must contain complete attention pages")
        num_reqs = metadata.num_reqs
        if num_reqs > self.max_num_reqs or num_reqs < 1:
            raise ValueError("draft batch exceeds the adapter's capacity")
        if getattr(metadata, "causal", False):
            raise ValueError("this adapter is validated for non-causal DSpark draft attention")
        full_table = metadata.block_table_tensor
        if full_table.shape[0] < num_reqs or full_table.shape[1] == 0:
            raise ValueError("full read table does not cover the draft batch")
        if full_table.device != self.device or metadata.seq_lens.device != self.device:
            raise ValueError("draft metadata and adapter devices differ")
        anchor_count = self.config.anchor_tokens // block_size
        max_columns = self.config.local_tokens // block_size + 1 + anchor_count
        plan = self.plans.get(block_size)
        if plan is None:
            lengths = metadata.seq_lens[:num_reqs]
            starts = (lengths - self.config.local_tokens).clamp(min=0) // block_size
            columns = torch.arange(max_columns, device=self.device).unsqueeze(0)
            anchors = self._anchors(block_size, num_reqs)
            if anchor_count:
                # Sorted anchors + trailing -1 padding make valid history a prefix.
                valid_anchors = (anchors >= 0) & (anchors < starts.unsqueeze(1))
                kept = valid_anchors.sum(dim=1).to(lengths.dtype)
                historical = anchors.gather(1, columns.clamp(max=anchor_count - 1).expand(num_reqs, -1))
                source_columns = torch.where(
                    columns < kept.unsqueeze(1), historical, starts.unsqueeze(1) + columns - kept.unsqueeze(1)
                )
            else:
                kept = torch.zeros_like(lengths)
                source_columns = starts.unsqueeze(1) + columns
            compact_lengths = lengths - starts * block_size + kept * block_size
            # Correctness-first baseline: one batched D2H per page size and step.
            # All groups of the same page size reuse this mirror. Profile this
            # explicitly before attempting deferred/async length optimizations.
            cpu_lengths = compact_lengths.to("cpu")
            self.cpu_length_copies += 1
            plan = (source_columns, columns, compact_lengths, cpu_lengths)
            self.plans[block_size] = plan
        source_columns, columns, compact_lengths, cpu_lengths = plan
        if compact_lengths.shape[0] != num_reqs:
            raise ValueError("groups within one step have inconsistent batch sizes")
        table_key = (group_id, block_size)
        table = self.tables.get(table_key)
        if table is None:
            table = torch.zeros((self.max_num_reqs, max_columns), dtype=full_table.dtype, device=self.device)
            self.tables[table_key] = table
        needed = (compact_lengths + block_size - 1) // block_size
        valid = (columns < needed.unsqueeze(1)) & (source_columns < full_table.shape[1])
        selected = full_table.gather(1, source_columns.clamp(min=0, max=full_table.shape[1] - 1))
        table[:num_reqs].copy_(selected * valid.to(selected.dtype))
        metadata.block_table_tensor = table[:num_reqs]
        metadata.seq_lens = compact_lengths
        for name in ("seq_lens_cpu", "_seq_lens_cpu", "seq_lens_cpu_upper_bound", "parallel_draft_seq_lens_cpu"):
            setattr(metadata, name, cpu_lengths)
        # These optimistic host lengths have already been replaced with exact
        # post-rejection device lengths; subtracting deferred rejects again is wrong.
        for name in ("parallel_draft_num_reject_cpu", "parallel_draft_num_reject_event"):
            if hasattr(metadata, name):
                setattr(metadata, name, None)
        metadata.max_seq_len = min(metadata.max_seq_len, max_columns * block_size)


def begin_dspark_history(proposer, metadata):
    """Called by the pinned source overlay once per draft metadata build."""
    additional = proposer.vllm_config.additional_config or {}
    settings = additional.get("research_history_kv")
    window = proposer.draft_window_size
    if settings is None and window is None:
        return None
    if proposer.method != "dspark" or proposer.use_compress:
        raise ValueError("history adapter requires uncompressed DSpark drafting")
    if proposer.use_cuda_graph:
        raise ValueError("this initial history adapter requires eager draft execution")
    if getattr(proposer, "dcp_size", 1) != 1:
        raise ValueError("context parallel history selection has not been validated")
    if settings is not None and window is not None:
        raise ValueError("configure one draft read policy, not two")
    config = HistoryConfig(**settings) if settings is not None else HistoryConfig(local_tokens=window, anchor_tokens=0)
    adapter = getattr(proposer, "_study_history_adapter", None)
    if adapter is None:
        adapter = PagedHistoryAdapter(config, proposer.runner.max_num_reqs + 1, proposer.device)
        proposer._study_history_adapter = adapter
    elif adapter.config != config:
        raise ValueError("draft read policy changed during the lifetime of an engine")
    adapter.begin_step(proposer.runner.requests, proposer.runner.input_batch.req_ids)
    return adapter
