# 实验记录：2026-10-17 至 2026-10-18

## 2026-10-18：切换服务器后的接入与诊断

用户提供了新的 SSH 环境。只使用密码建立连接，凭据不进入本项目文件、日志或 Git。新设备为一张 64GB Ascend910B2，数据盘 240GB；已有 CANN 9.1、Python 3.12.13、PyTorch 2.10 / torch-npu 2.10.0.post4 和 vLLM。非交互 SSH 默认使用的系统 Python 3.12.3 并不包含这些包，后续独立入口显式选用镜像中的正确 Python。

4×4 NPU 矩阵冒烟检查通过，共享 Qwen3-8B 的 16 个文件均与冻结的 ModelScope 清单匹配；只另行下载 DSpark Block-7，5 个文件全部校验通过。数据见 `evidence/server_b2/`。插件复制到 `/data/dspark-study-20261017/runtime`，原始包和共享模型均未修改。

新增整页读取适配器，10 项 CPU 逻辑测试在新服务器通过；实际 builder 的双 group / 混合长度 10 组 CPU 回归通过。它只改变 Draft 读集合，各 group 独立保存块表，沿用绝对写入位置。首版采用一次批量 D2H 长度镜像；这仍是待优化成本，不能先声称消除了准备开销。

| 尝试 | 可观察反馈 | 当前结论 |
| --- | --- | --- |
| 原生 AR 初始化 | worker 找不到 `libatb.so` | 独立启动脚本加载已有 ATB 环境后越过该阶段 |
| 原生编译 / 图模式 AR | `split_qkv_rmsnorm_rope_kernel` 报致命设备错误 | 历史适配器尚未启用；保存日志，核对 PID / startticks 后终止本实验故障进程 |
| 关闭单一 QKNorm / RoPE 融合 | 初始化与图捕获完成，后续图回放仍报异常 | 关闭该融合不能单独解决完整模型问题 |
| eager Target AR | 原生 `aclnnFusedInferAttentionScoreV3` 异常 | 不能归因于图模式或历史页选择；尚无合格 AR 参考输出 |
| 分页 FIA 首轮 | 8 组通过；三请求混合长度的第 9 组失败；后续未执行 | 部分读取数值正确不代表完整后端门槛通过；失败和准确源码均保留 |
| Full / Window / BM25 独立进程对照 | Full 也存在数值错误；部分稀疏调用通过，部分失败 | 当前证据不足以确定根因；继续比较同步、输入格式和软件配对 |

首轮 FIA 成功项最大绝对误差约 0.000422；这只是成功项的数值检查，不是性能结果或完整通过率。原始 [JSON](evidence/server_b2/paged_fia_initial.json)、[日志](evidence/server_b2/paged_fia_initial.log.gz) 和 [源码快照](archive/fia_initial/validate_paged_history_npu.py) 互相对应。后续独立对照也全部留存，不从失败批次中挑选成功项生成“加速”图。

同步 / 非同步同输入对照已经完成：Full KV 的两组各 3 次重复均有失败；同步 BM25 的两次也未通过。因此“打开同步就解决问题”的假设不成立。部分 Full KV 结果虽落在首版 `atol=0.02, rtol=0.02` 粗门槛内，最大误差仍达约 0.017–0.019，远高于首轮正常成功项；不能把这种边界通过解释为稳定正确。后续需收紧数值诊断，并报告误差分布。

随后比较 TND / BNSD、显式 ND 存储，以及 FIA 前后的块表和缓存保真。16 个独立进程中仍有通过与失败交替，单改布局或存储格式没有取得稳定通过。全部结果位于 [layout_controls](evidence/server_b2/layout_controls/fia_layout_controls_result.json)。调试配置的耗时不会混入正式性能。供应镜像的实际 vLLM commit 与插件仓库所记录的 verified commit 不同，后续还需核对这一差异，不能只凭版本号宣布兼容。

随后进一步移除了 attention：只执行小型长度传输、整数窗口计算、表传输、gather 与 mask。40 次中 36 次通过，4 次块表内容不一致；失败项的输入长度、窗口起点和压缩长度均与参考一致，构造器传输与持久 CPU 输入两种方式各有失败。见 [最小诊断](diagnose_npu_metadata.py)、[40 次记录](evidence/server_b2/metadata_without_attention.json)。这证明当前还有独立于 attention 的准备链问题；尚未证明它就是全部模型异常的共同根因。下一步需要单独比较表传输、gather 和 mask，以及输入／输出列数对齐。

Windows 本地直接加载 PyTorch 触发 OpenMP runtime 冲突，未使用允许重复 runtime 的绕过选项。17 项策略 / 指标 / 证据测试在本地通过，分页 CPU 测试在实际 Linux PyTorch 环境执行。Ruff 使用仓库固定的 0.14.0。以下旧记录保留第一阶段当时的环境与结论。

本次再次执行整仓 `format.sh ci`，在下载 `ruff-pre-commit` hook 时连接 GitHub 443 失败；聚合检查未通过。研究目录的独立 Ruff、codespell 和 markdownlint 检查通过。完整模型与整仓 NPU 测试未通过交付门槛，不能把这些独立检查写成整仓 CI 或模型正确性通过。

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
