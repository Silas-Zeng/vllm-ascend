# 基线接线与测量口径核验

本轮先测量、定位瓶颈，再研究历史 KV。AR 与 Full KV 使用服务器原有运行库的独立逐字节副本；
窗口组使用另一个副本中的最小接线修正。没有把窗口组写成未经修改的原生实现。
正式实验参数见 [协议](baseline_bottleneck_protocol.md)。

## 1. 窗口配置存在，但原生 DSpark 实际读表没有正确缩窗

服务器安装的 proposer 文件 SHA-256 为
`25f0e85ea44291448b8a1b91408d6e4f8c80636646b603f78dd1c3a8fdece01f`。
原仓库该文件无未提交变更，其 `git show HEAD` 的内容也匹配这个哈希。

实际调用顺序是：先在通用 metadata 上缩短长度和块表，随后 DSpark 的
`build_draft_attn_metadata` 又换回每个 KV group 的完整块表。
于是长度看起来约为窗口大小，FIA 却从完整表的开头读取，得到的是旧历史页。
只检查参数值或缩短后的 `seq_lens` 无法发现这个错误。

从真实 builder 提取函数后执行 44 项 CPU 检查，覆盖 11 类长度、两种块大小组合、两个 group。
原版本有 28 项失败；修正版 44 项通过。它是实际函数的隔离回归，不等于整套 vLLM 系统测试。
原生窗口的独立 NPU 读取审计中，160 次请求/group 调用全部出现读表不匹配，写地址均匹配。
因此原生窗口的计时不能作为“最近 2176 token”这条路径的性能证据。

修正版把缩窗移动到每个 group 的完整块表和原始写地址确定之后，并为 group 保留独立 adapter。
DSpark 当前 query 已包含七个位置，不再额外加六个未来位置。
修正版 SHA-256 为
`32b2eb3e883f434d39f058b06868ee0e88309d3e99b1144fa23b034b5e8ffc3e`。
初次修正后 NPU 审计的 55 次调用全部通过；正式矩阵还在每个投机配置计时结束后单独运行读取审计。
读取审计包含 CPU 拷贝和同步，完全排除在性能样本之外。

检查入口：[生成修正版与 CPU 回归](deployment/prepare_window_baseline.py)、
[实际 FIA 参数观察器](study_worker_hooks.py)、[独立绝对位置参考](audit_draft_reads.py)。
准备阶段原始汇总：[CPU](evidence/context_20261018/gates/window_patch_cpu_audit.json)、
[原生窗口 NPU](evidence/context_20261018/gates/native_window_live_audit.json)、
[修正版 NPU](evidence/context_20261018/gates/aligned_window_live_audit.json)。

### 实际读取范围

设本次 query 的最大绝对位置加一为 `D`，kernel 页大小为 `B=128`，窗口配置 `W=2176`。
独立参考检查如下：

```text
first_page = floor(max(D - W, 0) / B)
read_blocks = full_blocks[first_page : ...]
read_length = D - first_page * B
write_slot(position) = full_blocks[position // B] * B + position % B
```

长序列下实际读取长度为 2176–2303 token，受整页对齐影响；它不是严格的 2176 个 token。
绝对写入位置仍使用完整表，不能改为压缩窗口内的相对位置。
当前模型实际只有一个 Draft KV group，包含五个 Draft attention 层；双 group 的隔离回归用于覆盖接线风险。
K=7 指一次提议的 token 数，不能解释成 Draft 有七层。

Target 仍使用全上下文，底层 KV cache 仍按完整容量分配。缩短 Draft 的读取范围不等于降低全部 KV 常驻内存。

### 与当前官方主线的关系

核对时，官方主线已将 DSpark 的缩窗移到分组 metadata 构建中，说明服务器安装版本与主线不同。
这不能直接证明主线在本机组合下已经通过验证，也不应把已处理的接线问题作为当前主线的新发现重复提交。
本轮冻结原有环境，不在正式矩阵中途升级。
参见 [官方 proposer 源码](https://github.com/vllm-project/vllm-ascend/blob/ac5324b2a88cb2764a99582beebfe9ac3202c2ca/vllm_ascend/spec_decode/llm_base_proposer.py)。

## 2. 相同名义 token 容量并不代表相同调度预算

准备阶段都传 `max_num_batched_tokens=4096` 时，AR 实际可调度 4096 token，而单并发 DSpark 为 4090。
DSpark Block-7 的 anchor-first 路径需要额外六个槽位。这会改变恰好 4K 输入的 prefill 分块方式。
因此正式矩阵显式固定实际 `max_num_scheduled_tokens=4096`，并预留相应物理输入空间：

| 并发 | AR 物理容量 | DSpark 物理容量 | 两者实际调度预算 |
| --- | --- | --- | --- |
| 1 | 4096 | 4102 | 4096 |
| 4 | 4096 | 4120 | 4096 |

这是遵循当前 vLLM 配置校验的槽位预留，不是给 DSpark 增加实际 prefill 调度预算。
原始报告同时保存传入配置和解析后的配置；汇总器会拒绝预算不同的样本。
官方参数说明同样区分物理容量与实际调度量，见
[vLLM 调度参数](https://docs.vllm.ai/en/latest/cli/run-batch/)。

## 3. 输出一致性必须逐配置核验

早期 1K 诊断中的输出一致不能外推到 4K / 16K / 24K。
准备阶段在 BF16 下发现原生 Full KV 和 AR 的 greedy 输出分叉；对齐实际调度预算后仍有配置不一致。
故“调度预算不同”不是完整解释。

正式实验保留每次完整输出、同方法原生 `generate` 控制输出和 AR 参考输出。
每轮比较标记必须与实际 token 比较结果一致。输出长度、原生统计、配置和计数检查仍严格执行；
跨方法与同方法跨次差异均作为研究现象保留，不按输出一致性选择较快或较稳定的样本。
具体公共前缀以各配置原始 token 为准，不能把准备阶段的首次分叉位置套用到所有正式样本。

### 计时入口也需要核验

早期入口直接调用 `LLMEngine.add_request/step`，使用 `CUMULATIVE` 输出模式。
原生 `LLM.generate` 会先执行自己的批处理入口，并强制使用 `FINAL_ONLY`。
在预热 10 次的 4K / 并发 4 场景中，旧入口两次出现与原生控制输出不同的结果；
首轮是第一个请求从第 204 个 token 起分叉，其余三个请求一致。
不同输入入口、请求提交时序和输出模式同时存在，不能据此认定某一个因素是根因。

预热 3 次的 13 次隔离对照没有复现该差异，说明较弱的校验条件不能直接外推到正式条件。
随后完全不带 token 观察器、预热 10 次的纯原生重复生成，10/10 与控制输出一致。
在原生 `FINAL_ONLY` 中逐 token 观察的另一尝试，记录序列两次都与各自最终输出一致，
但跨次原生控制只匹配 1/2，仍存在采集扰动或批处理变化需要区分。
这些原始记录均保留，未作为最终正式性能样本。

最终采用 [native_request_metrics.py](native_request_metrics.py)：原样调用 `LLM.generate`，
只在请求完成的原生回调后读取已有 `RequestStateStats` 和 `FinishedRequestStats`。
不增加逐 token 处理，不修改请求、采样参数或模型计算。
相同 10 次预热条件下的两次 NPU 校验均与纯原生控制输出一致，且原生指标 token 数与最终输出相符。
最终正式矩阵全部统一使用这个入口，旧批次单独保留。

该入口下，九个并发 1 配置以及并发 4 的 AR 4K、16K 均完成十次测量。
随后并发 4 的 Full KV 4K 在第二次测量与自己的控制输出不同，窗口 4K 的第一次测量也不同。
实际输出仍各为 512 token，原生指标完整；两次尝试因最初的严格跨次检查退出，原始记录完整保留。
因此纯原生 AR 的稳定结果不能外推为投机解码在全部配置下稳定，也不能把所有差异归因于旧计时入口。

后续继续相同模型、软件、调度与原生统计入口，仅将跨次比较的处理改为记录差异、继续补足十次。
已完成的十一次配置逐字节复用，七个未完成配置完整重测；选择清单保存来源和 SHA-256。
这项改变发生在生成与计时之后，不改变请求或模型计算。严格尝试的部分样本单独展示，不混入十次正式中位数。
报告逐轮列出同方法控制和 AR 对照的一致次数、公共前缀范围，并展示全部输出变体。

原生 `arrival_time` 是墙上时钟；`queued_ts/scheduled_ts/first_token_ts/last_token_ts` 是单调时钟。
TTFT 直接采用原生 `first_token_latency`，TPOT 采用单调时钟的首末 token 差除以 511。
不能把墙上时钟和单调时钟的绝对值相减，也不能从总吞吐反推 TTFT / TPOT。
原生 corruption 标记是否有效还取决于运行时是否启用 logits NaN 检查；未启用时不能写成已做全量 NaN 校验。

官方文档指出，有限精度与批处理方式可能改变生成结果，但这只是本项目的候选解释；
缺少逐步 logits / margin 对照时，不能认定已经找到了分叉根因。
参见 [vLLM 投机解码说明](https://docs.vllm.ai/en/v0.8.3/features/spec_decode.html)。
当前 Ascend 的 batch invariance 功能也有使用限制，不能在固定条件实验中直接启用它改变整套配置。

因此本轮耗时比称为观察到的性能差异；只有输出一致性门槛通过的配置才具备相应的一致性证据。
恢复代码命中只是合成任务的一项局部检查，不代表开放任务质量，更不代替 token 一致性。

## 4. Profiling 的归因边界

每个配置单独采集前 64 个 worker 迭代，包含 prefill、混合批次与部分纯生成。
阶段标记直接来自实际 attention metadata 的 `num_prefills` / `num_decodes`，不按时间位置猜测阶段。
Target 有 36 层，Draft 有 5 层，Markov rank 为 256。

`operator_details.csv` 分别给出 host 与关联设备任务的累计耗时；两者不是同一种时间。
host 作用域可能包含设备同步等待。父子作用域重叠，不能相加后称为端到端耗时。
kernel 累计时长也不等于存在多流重叠时的经过时间。
此外，capture 的 64 次迭代不等于整段 512-token 生成，不能拿完整输出的接受计数为局部 trace 做精确分摊。

试采集进一步发现：当前 TorchNPU 的 CPU 作用域设备关联没有包含 ACL 图回放内部 kernel。
例如 Target 图回放的该列可为零，实际 NPU 图任务仍在执行。
最终 Target 归因使用 kernel 表中的图 model ID 累计时长，并核验每次实际回放对应 36 个 FIA kernel；
eager Target 部分仍使用其作用域关联时长，避免遗漏图回放或重复相加。

细粒度分类采用 attention metadata 的执行路径定义。短尾 prefill、启动步可能进入小 query 路径，
不应将它与请求语义上的 prefill / decode 边界混同；后者同时由原生请求统计给出。
性能样本逐轮记录同方法控制比较，差异样本不筛除；独立 profiling 同样记录采集条件下的差异，不将其伪装为一致性通过。

官方已有 DSpark 主机同步的相关报告，可作为需要核验的线索：
[Qwen3 / Qwen3.5 同步问题](https://github.com/vllm-project/vllm-ascend/issues/16271)、
[MRV1 图模式支持说明](https://github.com/vllm-project/vllm-ascend/issues/17018)。
本机结论仍须以本轮阶段表和时间线为依据；不会直接套用其他机器的瓶颈结论。
