# 正式矩阵与阶段图的复核方法

先用原始证据复算图表，再考虑重跑 NPU。模型权重、SSH 凭据和服务器登录配置不放入仓库。
所有命令从本研究目录执行。

## 无 NPU 复算

```bash
python -m pip install -r requirements-context-analysis.txt
python verify_context_archive.py evidence/context_20261018
```

校验器检查 manifest 列出的下载文件及 gzip 展开内容的大小、SHA-256，并核对正式计时、
profiling 和 SDK 后处理任务均已成功结束。清单中的服务器 SDK 原始文件保留在服务器，
只保存其完整哈希目录；它们不等于已经全部下载。

需要与冻结模型相同的 tokenizer，下载到仓库之外：

```bash
curl --fail --location --output /tmp/Qwen3-8B.tokenizer.json \
  https://modelscope.cn/models/Qwen/Qwen3-8B/resolve/8188480f040c5f1606a1bb3556abf14b31975155/tokenizer.json
python reproduce_context_analysis.py \
  --evidence evidence/context_20261018 \
  --tokenizer /tmp/Qwen3-8B.tokenizer.json \
  --output results/context_20261018 \
  --figures figures/context_20261018
```

脚本先对 tokenizer 核对冻结模型清单，再复算 18 个配置的统计、全部输出变体、阶段表和时间线。
CSV 在临时目录解压，原始证据保持不变。图中误差棒为十次批次测量的 Q25–Q75；
并发 4 的四个请求不冒充四个独立批次样本。

## 冻结源码与原始任务

`evidence/context_20261018/state/` 保存实际任务的命令、来源 SHA-256、启动及终止状态；
`sources/` 保存按内容哈希命名的 gzip 源码快照。复核实际执行版本时，以这些快照为准。
`logs/` 保存压缩原始日志，`profiles/` 保存全部配置的 CSV 和原始时间戳事件。

正式数据由两个任务组成：第一轮已完整结束的 11 个配置逐字节复用，第二轮完成剩余 7 个配置。
选择清单保存来源路径与哈希。早先严格控制检查退出的部分样本保存在
`strict_native_attempt/`，没有混入正式十次中位数，也没有删除不一致输出。

仓库的 `study_worker_hooks.py` 在运行冻结之后仅增加说明性 docstring；
实际执行的冻结版本为 `28eb4a19d102…`，可从 `sources/` 恢复。
`run_vllm_baseline.py` 与 `native_request_metrics.py` 的实际 SHA-256 分别为
`03eb6ad10a9c…`、`c2723224f151…`。当前任务生成器额外暴露了记录跨次差异的 CLI 选项，
用于完整重跑；本轮实际生成命令仍以归档的 spec 为准。

## 同条件重跑 NPU

使用独立实验目录，保留 [协议](baseline_bottleneck_protocol.md) 规定的软件、模型和原生执行条件。
本轮供应环境的实际 commit 与插件声明的 verified commit 不同，完整兼容性尚未确认；
不可将换用较新官方镜像后的数据直接并入本轮结果。

在已准备好原运行库副本、冻结 study stage、模型及相同输入文件的实验目录中：

```bash
python3 study/deployment/make_context_job.py \
  --root /data/dspark-study-20261017 \
  --stage /data/dspark-study-20261017/stages/context_native_stats_v7/study \
  --window-runtime /data/dspark-study-20261017/runtimes/window_baseline_v1 \
  --output /data/dspark-study-20261017/evidence/reproduction_timing \
  --name context_reproduction_timing \
  --phase timing --timing-method native_stats --record-control-mismatch --launch
```

这是 18 个配置与 12 个独立短输出读取审计的串行任务。新任务必须使用新的名称与输出目录。
`--record-control-mismatch` 只改变生成结束后对比较结果的处理：记录真实差异并继续采样；
不改变模型、采样、调度或原生请求统计。输出长度、计数或原生 corruption 错误仍会失败。

完成全部计时和读取审计后，使用同一生成器的 `--phase profile`，
另指定 `--reference-directory` 为计时输出目录，以及全新的任务名和输出目录。
每配置预热 3 次、控制生成 1 次、采集生成 1 次，保留前 64 个 worker 迭代。
阶段标签来自 attention metadata 的执行路径；语义 prefill / decode 同时使用原生请求统计解释。

18 个 NPU 采集配置全部结束后，再运行 `deployment/make_profile_analysis_job.py`，
将原生 SDK 数据串行解析为 CSV。不要在正式计时或采集期间并发执行 SDK 后处理。
最后使用 `deployment/collect_context_evidence.py` 封存终态任务；
只在模型、输入、配置、计时入口及源码均匹配时比较结果。

## 检查范围

本研究的 CPU 数据合同、输出标记、时钟、图归因、下载校验与部署隔离有独立回归测试；
窗口修正另有真实 builder 的 44 项隔离检查及逐配置 NPU 读取审计。
这些不等于完整 vLLM 分布式或模型精度测试。

仓库格式检查的真实结果与平台限制在最终报告中列明。原始 kernel 名称、日志或冻结源码
不得为了通过拼写、换行或格式检查而修改；必要时只调整派生展示或限定检查范围。
