# 接入真实 DSpark 前必须完成的工作

## 软件和源码基线

研究分支以用户 fork 的 `0e181fc85fff24db10e5f92f10319c1913e6e9c5` 为基础。实际 NPU 测量使用独立脚本和 torch-npu 2.7.1.post4，没有在服务器安装这份 vLLM-Ascend。

当前官方主分支的兼容组合要求 CANN 9.1.0、PyTorch 2.10.0、torch-npu 2.10.0.post4 等；应完整选择兼容组合并固定 vLLM / vLLM-Ascend commit，不能仅升级一个 Python 包。[官方版本矩阵](https://docs.vllm.ai/projects/ascend/en/main/community/versioning_policy.html)、[官方安装说明](https://docs.vllm.ai/projects/ascend/en/main/getting_started/installation.html)。

另核查了面向 CANN 9.0 的 `v0.21.0rc1`：官方仓库递归文件树中未检出路径包含 `dspark` 的文件，不能把它当作已验证的 DSpark 环境。当前主机 glibc 2.31，而该版本公开 aarch64 wheel 标记为 `manylinux_2_34`，也不能直接假定该 wheel 可用。这里未升级宿主驱动或替换全局 CANN。

模型应固定 Target `Qwen/Qwen3-8B` 与其匹配的 `deepseek-ai/dspark_qwen3_8b_block7`，记录 revision、配置和权重校验信息。官方发布的 checkpoint 与训练目标和推理模式有关，不能任意替换草稿模型后沿用接受率结论。[DeepSpec 官方模型说明](https://github.com/deepseek-ai/DeepSpec#released-checkpoints)。

## 框架已有滑窗与待实现的历史锚点

草稿滑窗已经是框架功能，配置入口为 `draft_window_size`；其 MRV1 与 MRV2 能力需按实际 commit 核查。本项目新增的是研究原型，未宣称滑窗配置为原创功能。[官方滑窗设计](https://github.com/vllm-project/vllm-ascend/blob/main/docs/source/developer_guide/Design_Documents/speculative_kv_sliding_window.md)。

接入历史锚点时，应分别验证以下约束：

1. 只改变 Draft 的读取集合，保留 Target 的完整上下文验证和正确的 rejection / greedy 验证逻辑。
2. 保持 KV 写入的原始 `slot_mapping` 与 RoPE 绝对位置，不能用压缩后的序号替代真实位置。
3. 在 DSpark 按 KV group 重建块表之后应用选择，避免被后续 metadata 构建覆盖；每组都核对真实 page size。
4. 明确 DSpark `seq_lens` 是否已包含 bonus / mask query，防止遗漏、重复加入或错误读取未来位置。
5. 同步更新设备长度和所有 CPU 镜像、upper bound、padding 信息；检查 graph capture 的静态 buffer 和 replay 地址。
6. 128-token 检索块不一定等于 attention kernel 的页。部分页需要显式 gather / offset 支持，或改成页级读取并记录真实 token 预算，不能只改块表后声称严格 2176-token 预算。
7. 只缓存请求级历史锚点。局部窗口随 decode 步推进；prompt、问题、tokenizer、模型 revision 或预算改变时需正确失效缓存。
8. 路径必须通过同一输入的 AR / Full KV / Window / Anchors greedy token 一致性回归；采样分布和拒绝采样验证是另一组检查，不能用组件精度替代。

目前的 `physical_addresses` 是正确性参考，不是 FIA metadata adapter。组件原型通过 materialize K/V 实现读取，真实块表直读是否能消除 gather，仍需后续实现和 Profiling。

## 已准备的真实模型基线入口

[run_vllm_baseline.py](run_vllm_baseline.py) 提供 AR / Full KV / Window 三种模式，禁用 prefix caching、固定 BF16、K=7、贪心解码，使用相同的 `FULL_DECODE_ONLY` 配置。历史锚点尚未接入，所以入口明确拒绝该模式。

需要提供已经按 Target tokenizer 编码的请求文件，示意结构如下；请用实际 tokenizer 产生 token ID，不要把这些示意数字当作实验输入：

```json
[
  {"request_id": "request-0", "prompt_token_ids": [123, 456]},
  {"request_id": "request-1", "prompt_token_ids": [789, 1011]}
]
```

每组文件的请求数须等于并发数。保存输入的 SHA-256、实际 token 长度、任务类别和 tokenization 设置；4K / 16K / 24K 应使用实际编码后的长度。问题文本应单独保留用于检索，不能拿正确答案或生成结果指导历史选择。

准备好兼容环境和本地权重后，以并发 4 为例运行：

```bash
python run_vllm_baseline.py --mode ar --target /models/Qwen3-8B --prompts prompts_24k.json --output evidence/e2e_ar.json
python run_vllm_baseline.py --mode full --target /models/Qwen3-8B --draft /models/dspark_qwen3_8b_block7 --prompts prompts_24k.json --reference evidence/e2e_ar.json --output evidence/e2e_full.json
python run_vllm_baseline.py --mode window --target /models/Qwen3-8B --draft /models/dspark_qwen3_8b_block7 --prompts prompts_24k.json --reference evidence/e2e_ar.json --output evidence/e2e_window.json
```

这些命令尚未在本次服务器执行成功；不能把配置单测通过写成真实模型回归通过。正式实测时还需固定模型 revision、软件 commit、可用设备与环境快照，并验证所有 vLLM API 与实际版本一致。

入口报告的是包括 prefill 的离线 output tok/s。只有 request metrics 提供有效 first-token / finish 时间时才记录 TPOT；缺失时保留 `null`，不从吞吐倒推。

Mean acceptance length 应明确是否包含 bonus token，并从同一测量区间的原始 accepted-token / verification-step 计数计算；不要平均不同负载区间的日志平均值。当前官方约定包含 bonus token，为 `1 + accepted_draft_tokens / spec_steps`。[vLLM 指标定义](https://docs.vllm.ai/en/latest/features/speculative_decoding/acceptance_metrics/)。

最终报告必须同时展示接受变化、Draft backbone、Markov Head、Target Verification、检索与索引准备，以及不带 Profiler 的端到端结果。接受率提高但整体变慢，也是有效结论。
