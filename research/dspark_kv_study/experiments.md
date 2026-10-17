# 实验记录：2026-10-17

## 接入和范围核实

使用本机已有 SSH 连接到 AtomGit Ascend 设备；运行时报告 `Ascend910_9362`。最初非交互 shell 缺少驱动库搜索路径，`npu-smi` 无法加载 `libc_sec.so`；在独立子进程加载已有 CANN 环境并补充 driver library path 后，设备查询与 torch-npu 可用性检查成功。

本机已有 GitHub 登录有效，当前账号为 `Silas-Zeng`。本地旧研究仓库 `DeepSeek-V4.1-Performance-Engineering` 的远端返回 404，因此研究材料提交到已有管理权限的 `Silas-Zeng/vllm-ascend` 独立研究分支，未创建新仓库或向上游提交 PR。

现有服务器只有算子项目，未找到已部署的 DSpark 服务。常见模型缓存目录没有目标权重；Hugging Face 配置直连出现网络错误，PyPI 元数据可达。没有把“某次网络错误”解释成所有模型下载渠道都不可用，但软件兼容组合仍需补齐。

## 候选与取舍

| 候选 | 原始证据 | 结论 |
| --- | --- | --- |
| 固定 context 下缓存全部选中位置 | [run_03](evidence/run_03.json)、[run_04](evidence/run_04.json) | 仅作为固定索引开销对照，不能直接代表逐步解码的窗口更新 |
| 只缓存历史锚点，每次重建局部窗口索引 | [run_06](evidence/run_06.json)、[run_07](evidence/run_07.json) | 主报告采用该版；局部索引准备和 gather 均计入 |

父候选完整源码保存在 [archive](archive/benchmark_attention_fixed_context.py)，其同目录 `history_policy.py` 与原始 hash 对应。父候选与当前脚本不能混合汇总，绘图入口会拒绝不同源码 hash 的两轮数据。

服务器的独立实验目录不是 Git checkout，因此 JSON 中的 `source_revision` 为 `null`。复现依靠已记录的 benchmark / policy 文件 SHA-256；交付前核对其与仓库源码逐字节一致。用户 fork 的基线 commit 仅标明研究分支来源，不冒充服务器安装版本。

父候选 Batch 4、24K 的缓存组件中位数约 0.287 ms；当前版本约 0.317 ms。两批实验在不同时刻执行，不能把约 0.030 ms 的差值当作严格隔离出来的窗口更新耗时。主报告以更完整的逐次准备路径为准。

## Profiling 修正与原始记录

最初两次尝试完成计时后，在 Profiling 后处理失败：当前 torch-npu 的 `profile` 没有 PyTorch `key_averages()`，且导出的 Chrome trace 顶层是事件数组。随后修正为显式 schedule 和兼容两种 trace 结构的解析，并加入格式回归测试。

失败尝试的 `run_01.json`、`run_02.json` 仍保存在服务器独立实验目录，未混入主图。动态窗口版的探索运行 `run_05.json` 同样保留；最终冻结源码后以 run_06 / run_07 生成主报告。未删除原始文件，没有正式竞赛提交。

已提交的主 trace 是 gzip 压缩的原始导出，解压后 SHA-256 应与 [run_06.json](evidence/run_06.json) 的 `profiling.trace_sha256` 一致。CPU/NPU 时间线只取真实 `Ascend Hardware` kernel，不把 host 作用域误标为设备执行。

## 验证与限制

- 本地 14 个研究测试通过，涵盖选择语义、地址映射、TPOT 定义、trace 格式和证据完整性。
- 研究目录通过固定版本 Ruff 0.14.0、codespell 2.4.1 和 markdownlint 0.45.0 检查。整仓 `format.sh ci` 已尝试，但 pre-commit 从 GitHub 拉取 hook 仓库时连接失败，聚合检查未完成。
- 服务器上选择策略与指标合同的 11 个测试通过；绘图和证据测试在本地执行。
- 最终两轮 6 个形状、4 条路径的所有数值检查通过，原始样本全部保留。
- 没有运行整仓 vLLM 模型 / 分布式测试：当前环境缺少所需推理依赖和权重；本次未修改生产实现。
- 两轮测量不等于跨设备统计。Batch 1 的部分结果存在明显轮间波动，主文明确展示方向变化。

## 下一项实验

先准备匹配版本的 DSpark 推理环境，验证 AR / Full / Window 基线；再接入真正的历史块表读取，检查 RoPE、slot mapping、CPU 长度镜像、页对齐和 graph replay。随后测量接受率与完整吞吐，决定是否继续优化 non-contiguous gather。当前没有证据把简历的猜测数字改写成已取得的模型成果。
