# 真实 DSpark 接入、运行环境与回归门槛

## 软件和源码基线

研究分支以用户 fork 的 `0e181fc85fff24db10e5f92f10319c1913e6e9c5` 为基础。第一阶段组件测量使用独立脚本和 torch-npu 2.7.1.post4；它与后续完整模型的运行库是不同基线。

后续首先在旧服务器通过官方固定 OCI 镜像准备了 CANN 9.1 / PyTorch 2.10 推理栈。该开发环境随后被平台停止，用户提供了新服务器。新设备上已有的镜像、软件版本、实际源码路径与 SHA-256 已重新核查，见 [runtime_probe.json](evidence/server_b2/runtime_probe.json)、[source_manifest.json](evidence/server_b2/source_manifest.json)。不能把同一个版本标签当成同一份软件，或混合两台设备的性能样本。[官方版本矩阵](https://docs.vllm.ai/projects/ascend/en/main/community/versioning_policy.html)、[官方安装说明](https://docs.vllm.ai/projects/ascend/en/main/getting_started/installation.html)。

新服务器为 Ascend910B2、64GB，CANN 9.1、torch-npu 2.10.0.post4，glibc 2.39。vLLM 源码为 `568afb3a13806beb53bb2e6bd518269357b237c0`，Ascend 插件为 `85625bd772f27a4a13bc8e548f131af5ce3ff734`，安装标签另见环境记录。插件仓库中记录的 verified vLLM commit 与当前源码不同，仍需进一步核对兼容性。原始包被复制到独立目录，通过进程级 `PYTHONPATH` 使用；共享权重仅做只读校验，没有升级宿主驱动或替换全局 CANN。

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

`physical_addresses` 是 token 级正确性参考。新增 [paged_history.py](paged_history.py) 则直接构建 Draft 读块表，不物化所选 K/V；仍有块表 gather 和一次批量 CPU 长度同步，其性能不能假定为零。最小源码覆盖脚本 [apply_history_patch.py](deployment/apply_history_patch.py) 只接受两份已核查源码的 SHA-256，并要求导入路径位于显式指定的隔离目录。

覆盖脚本在写入前执行实际 builder 的双 KV group 回归，保存原始源码备份和 diff。当前 [builder 检查](evidence/server_b2/history_builder_qa.json) 的 `applied` 为 `false`；真实 FIA 检查尚有失败，暂不将候选晋升为模型通过。首次 FIA 检查的准确源码和全部依赖位于 [archive/fia_initial](archive/fia_initial/validate_paged_history_npu.py)，与原始 SHA-256 一致。

## 已准备的真实模型基线入口

[run_vllm_baseline.py](run_vllm_baseline.py) 提供 AR / Full KV / Window / Anchors 模式，禁用 prefix caching、固定 BF16、K=7、贪心解码。默认 Target 为 `FULL_DECODE_ONLY`，Draft 为 eager；`--target-eager` 是显式诊断对照，不能与图模式基线混为一组。Window / Anchors 要求先安装经过校验的覆盖，缺失时直接报错，避免实际跑 Full 却标成稀疏模式。

需要提供已经按 Target tokenizer 编码的请求文件，示意结构如下；请用实际 tokenizer 产生 token ID，不要把这些示意数字当作实验输入：

```json
[
  {"request_id": "request-0", "prompt_token_ids": [123, 456], "query_token_ids": [456]},
  {"request_id": "request-1", "prompt_token_ids": [789, 1011], "query_token_ids": [1011]}
]
```

每组文件的请求数须等于并发数。保存输入的 SHA-256、实际 token 长度、任务类别和 tokenization 设置；4K / 16K / 24K 应使用实际编码后的长度。问题文本应单独保留用于检索，不能拿正确答案或生成结果指导历史选择。

准备好通过验证的环境和本地权重后，以并发 4 为例运行：

```bash
python run_vllm_baseline.py --mode ar --target /models/Qwen3-8B --prompts prompts_24k.json --output evidence/e2e_ar.json
python run_vllm_baseline.py --mode full --target /models/Qwen3-8B --draft /models/dspark_qwen3_8b_block7 --prompts prompts_24k.json --reference evidence/e2e_ar.json --output evidence/e2e_full.json
python run_vllm_baseline.py --mode window --target /models/Qwen3-8B --draft /models/dspark_qwen3_8b_block7 --prompts prompts_24k.json --reference evidence/e2e_ar.json --output evidence/e2e_window.json
```

这些示例不代表已通过的完整模型结果。当前新镜像的原生 AR 也出现 attention 异常，需先解决；不能把配置单测、部分 FIA 数值检查或模型成功加载写成生成回归通过。`--disable-qknorm-rope` 使用框架已有配置关闭单一融合做对照，参考 AR 与候选必须使用相同融合、EOS 和 Target 执行模式。

入口报告包括 prefill 的离线 output tok/s。默认通过真实 engine cumulative 输出事件记录 TTFT 和 TPOT；一次投机事件可能带来多个 token，TPOT 除以输出 token 间隔数，不能除以事件个数。`--timing-method generate` 保留原生离线接口对照，原生 request metrics 缺失时 TPOT 为 `null`，不从吞吐倒推。

Mean acceptance length 应明确是否包含 bonus token，并从同一测量区间的原始 accepted-token / verification-step 计数计算；不要平均不同负载区间的日志平均值。当前官方约定包含 bonus token，为 `1 + accepted_draft_tokens / spec_steps`。[vLLM 指标定义](https://docs.vllm.ai/en/latest/features/speculative_decoding/acceptance_metrics/)。

最终报告必须同时展示接受变化、Draft backbone、Markov Head、Target Verification、检索与索引准备，以及不带 Profiler 的端到端结果。接受率提高但整体变慢，也是有效结论。

## 部署与实验工具

- [download_models.py](deployment/download_models.py)：冻结 ModelScope 每个文件的 revision / SHA-256，断点下载后逐一校验；`--model` 可只补齐草稿模型。
- [verify_checkpoint.py](deployment/verify_checkpoint.py)：对已有共享模型做只读校验，避免重复下载或修改权重。
- [run_native.sh](deployment/run_native.sh)：适配本次供应镜像的 Python 3.12.13、CANN 和 ATB 路径，只设置当前子进程环境。
- [run_job.py](deployment/run_job.py)：顺序运行冻结的命令、保存源码快照和原始退出状态；发生超时时终止本步骤独立进程组。诊断批次可继续收集后续失败，最终明确标为 `completed_with_failures`。

同步诊断的 `ASCEND_LAUNCH_BLOCKING=1` 只用于定位错误，不能用于发布正式性能。旧服务器的 daemon-free OCI 部署脚本也保留在 `deployment/`，不要求新服务器重新安装它。
