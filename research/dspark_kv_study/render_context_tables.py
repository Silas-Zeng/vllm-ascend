"""Render readable tables from the validated complete context summaries."""

import argparse
import json
from itertools import groupby
from pathlib import Path

LABELS = {"ar": "AR", "full": "DSpark Full KV", "window": "Window 2176（接线修正）"}


def require_complete(items):
    expected = {(method, context, batch) for method in LABELS for context in (4096, 16384, 24576) for batch in (1, 4)}
    actual = {(item["method"], item["context"], item["concurrency"]) for item in items}
    if len(items) != 18 or actual != expected:
        raise ValueError("cannot render an incomplete or duplicate matrix")


def interval(item, digits=3):
    return f"{item['median']:.{digits}f} [{item['q25']:.{digits}f}, {item['q75']:.{digits}f}]"


def macro_tables(items):
    require_complete(items)
    lines = [
        "# 正式性能表",
        "",
        "每组 10 次预热、10 次测量。方括号为 Q25–Q75；不是置信区间。",
        "",
        "总耗时与吞吐含 prefill；模型加载和编译初始化不计入。TTFT / TPOT 直接读取原生请求统计。",
        "并发 4 的吞吐是四个请求合计；TTFT / TPOT 先取每批请求的中位数，再汇总十次重复。",
        "",
        "耗时比为 AR 中位数 / 当前方法中位数。跨方法输出不一致时，该比值不代表已验证的无损加速。",
        "",
    ]
    for batch in (1, 4):
        lines += [
            f"## 并发 {batch}",
            "",
            "| 输入 | 方法 | 总耗时 s [IQR] | 输出吞吐 token/s | TPOT ms | TTFT ms | "
            "接受长度 | AR/当前耗时 | 与 AR 一致的请求次 | 同方法控制一致的批次 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for item in (row for row in items if row["concurrency"] == batch):
            accepted = item["nominal_accepted_length"]
            accepted_text = f"{accepted:.3f}" if accepted is not None else "—"
            lines.append(
                f"| {item['context'] // 1024}K | {LABELS[item['method']]} | {interval(item['wall_seconds'])} | "
                f"{item['throughput_tokens_s']['median']:.2f} | {item['batch_median_tpot_ms']['median']:.3f} | "
                f"{item['batch_median_ttft_ms']['median']:.2f} | {accepted_text} | "
                f"{item['wall_speed_ratio_vs_ar']:.3f}× | "
                f"{item['ar_exact_request_runs']}/{10 * batch} | {item['native_control_matching_batches']}/10 |"
            )
        lines.append("")
    lines += [
        "接受长度 = 1 + 测量期间 accepted draft tokens / request draft rounds，包含 bonus token。",
        "尾部截断会使该指标与实际每次输出的 token 数略有差异；原始计数均保留。",
        "",
        "一致性按十次测量逐次统计；出现差异的样本全部保留，未按输出或耗时挑选测量轮次。",
        "",
    ]
    return "\n".join(lines)


def request_tables(items):
    require_complete(items)
    lines = [
        "# 请求级时间与输出检查",
        "",
        "时间为十次重复的中位数；完整波动见 context_summary.json。",
        "",
        "prefill / decode 是原生请求的语义阶段间隔，包含调度与等待，不等于 NPU kernel 累计时间。",
        "",
        "| 并发 | 输入 | 方法 | 请求 | TTFT ms | TPOT ms | 排队 ms | Prefill ms | Decode ms | "
        "AR 公共前缀范围 token | AR 一致次数 | 恢复代码命中次数 | 同方法控制一致次数 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    texts = [
        "# 全部原生输出变体",
        "",
        "每请求展示十次测量中的全部 token 序列变体及对应轮次（从 0 开始）。每次固定 512 token；末尾可能截断。",
        "与 AR 对照时使用对应配置的 AR 首轮完整输出；同方法控制是预热后、测量前另生成的一次完整输出。",
        "",
        "恢复代码命中是合成任务的局部检查，不能代替完整 token 一致性或一般任务质量。",
        "",
    ]
    for item in items:
        for quality, metrics in zip(item["quality"], item["per_request_metrics"]):
            label = (
                f"C{item['concurrency']} / {item['context'] // 1024}K / {LABELS[item['method']]} / "
                f"请求 {quality['request']}"
            )
            cells = [
                f"{metrics[key]['median']:.3f}"
                for key in ("ttft_ms", "tpot_ms", "queued_ms", "prefill_ms", "decode_ms")
            ]
            lines.append(
                f"| {item['concurrency']} | {item['context'] // 1024}K | {LABELS[item['method']]} | "
                f"{quality['request']} | "
                + " | ".join(cells)
                + f" | {quality['ar_common_prefix_min']}–{quality['ar_common_prefix_max']} | "
                f"{quality['ar_exact_runs']}/10 | {quality['recovery_code_found_runs']}/10 | "
                f"{quality['native_control_matching_runs']}/10 |"
            )
            for variant in quality["output_variants"]:
                text = variant["decoded_output"]
                longest_backticks = max(
                    (sum(1 for _ in characters) for character, characters in groupby(text) if character == "`"),
                    default=0,
                )
                fence = "`" * max(3, longest_backticks + 1)
                iterations = ", ".join(map(str, variant["iterations"]))
                texts += [
                    f"<details><summary>{label}；轮次 {iterations}</summary>",
                    "",
                    fence + "text",
                    text,
                    fence,
                    "",
                    "</details>",
                    "",
                ]
    return "\n".join(lines) + "\n", "\n".join(texts)


def profile_tables(items):
    """Show cumulative device tasks and nested host scopes as separate measurements."""
    require_complete(items)
    lines = [
        "# 独立阶段 profiling 表",
        "",
        "每配置另采一次前 64 个 worker 迭代，包含大块 prefill、混合路径和部分小 query 生成/验证。",
        "下表 NPU 时间是累计任务时长；host 时间包含等待。二者不能相加当成端到端耗时。",
        "",
        "attention metadata 的执行路径分类与请求语义阶段边界不完全相同；短尾和启动步可能进入小 query 路径。",
        "",
        "Target 图回放内部 kernel 未完整关联到 CPU 作用域，已使用图 model ID 与 36 层 FIA 调用数修复归因。",
        "",
        "| 配置 | Prefill / mixed / 小 query 调用数 | Target forward NPU ms/小 query 调用 | "
        "图 FIA NPU ms/回放 | 图回放数 | 图 FIA 数 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    draft = [
        "",
        "## Draft 小 query 路径",
        "",
        "每调用指一次 batch 的 Draft 调用；并发 4 时不能把它当成单请求成本。Attention 为五层合计，属于 backbone 子集。",
        "",
        "| 配置 | 调用数 | Draft NPU ms/调用 | Attention NPU ms/调用 | LM head NPU ms/调用 | "
        "Markov embed+bias NPU ms/调用 | metadata host ms/调用 | Draft host ms/调用 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    draft_host = [
        "",
        "## Draft 主机作用域",
        "",
        "以下均为 host 总时长 / batch Draft 调用，含等待；"
        "Generate 包含 backbone、LM head 和 Markov 子作用域，不能相加。",
        "",
        "| 配置 | 输入 / metadata ms | Context KV ms | Backbone ms | Generate（父）ms | "
        "LM head ms | Markov embed+bias ms |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    target_post = [
        "",
        "## Target forward 之后的路径",
        "",
        "均按小 query Target 调用数归一化；— 表示没有对应作用域记录，不能解释为设备零成本。",
        "采样与拒绝采样可能包含嵌套子作用域，不能简单相加。",
        "",
        "| 配置 | Logits host / NPU ms | Sampler host / NPU ms | Rejection sampler host / NPU ms |",
        "| --- | --- | --- | --- |",
    ]
    for item in items:
        phases = item["phase_counts"]
        target = item["target_attribution"]
        graph_calls = sum(value["graph"] for value in target["execution_counts"].values())
        lines.append(
            f"| {item['label']} | {phases['prefill']} / {phases['mixed']} / {target['pure_target_calls']} | "
            f"{target['pure_forward_device_ms_per_call']:.3f} | {target['graph_fia_ms_per_replay']:.3f} | "
            f"{graph_calls} | {target['graph_fia_calls']} |"
        )
        phase = "decode" if item["method"] == "ar" else "verify"
        values = []
        for name in ("target_logits", "target_sampler", "rejection_sampler"):
            data = item["scopes"].get(f"study/{name}/{phase}")
            if data is None:
                values.append("—")
            else:
                values.append(
                    " / ".join(
                        f"{data[field] / target['pure_target_calls'] / 1000:.3f}" if data[field] is not None else "—"
                        for field in ("host_total_us", "device_total_us")
                    )
                )
        target_post.append(f"| {item['label']} | " + " | ".join(values) + " |")
        if item["method"] != "ar":
            scopes = item["scopes"]
            root = scopes["study/draft_total/decode"]
            n = root["calls"]
            device = lambda name, scopes=scopes, n=n: scopes["study/" + name + "/decode"]["device_total_us"] / n / 1000
            draft.append(
                f"| {item['label']} | {n} | {device('draft_total'):.3f} | {device('draft_attention'):.3f} | "
                f"{device('draft_logits'):.3f} | {device('markov_embed') + device('markov_bias'):.3f} | "
                f"{scopes['study/draft_metadata/decode']['host_total_ms_per_call']:.3f} | "
                f"{root['host_total_ms_per_call']:.3f} |"
            )
            host = lambda name, scopes=scopes, n=n: scopes["study/" + name + "/decode"]["host_total_us"] / n / 1000
            draft_host.append(
                f"| {item['label']} | {host('draft_inputs'):.3f} / {host('draft_metadata'):.3f} | "
                f"{host('draft_context_cache'):.3f} | {host('draft_backbone'):.3f} | {host('draft_generate'):.3f} | "
                f"{host('draft_logits'):.3f} | {host('markov_embed') + host('markov_bias'):.3f} |"
            )
    return "\n".join(lines + draft + draft_host + target_post) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    items = json.loads(args.summary.read_text(encoding="utf-8"))["configurations"]
    (args.output / "context_tables.md").write_text(macro_tables(items), encoding="utf-8")
    requests, outputs = request_tables(items)
    (args.output / "context_request_details.md").write_text(requests, encoding="utf-8")
    (args.output / "context_outputs.md").write_text(outputs, encoding="utf-8")
    if args.profiles:
        profiles = json.loads(args.profiles.read_text())["configurations"]
        (args.output / "context_profile_tables.md").write_text(profile_tables(profiles), encoding="utf-8")
