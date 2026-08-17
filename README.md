# MemoryOS

> [开发问题总复盘：开发中遇到的问题、失败实验、修复证据与遗留风险](docs/DEVELOPMENT_PROBLEMS_RETROSPECTIVE.md)
>
> [V2.3 最小充分上下文：瘦响应、Token 分账、Context Atom、Delta、证据门禁与当前限制](docs/MINIMUM_SUFFICIENT_CONTEXT.md)
>
> [V2.2 Hardening Report：H-001～H-008 修复、迁移、测试、基准与限制](HARDENING_REPORT.md)
>
> [DeepSeek Harness 实测总览：测试矩阵、失败根因、修复与证据边界](docs/DEEPSEEK_HARNESS_EVIDENCE.md)
>
> 独立 DSH 插件：[tianhao8687/dsh-memoryos](https://github.com/tianhao8687/dsh-memoryos)

<!-- MEMORYOS:READINESS:START -->
### AI calibration readiness (自动生成)

> 单一事实源: `benchmarks/ai_calibration_v1/readiness.json`。请运行 `python scripts/sync_project_status.py --write` 更新本段; 不要手工修改。

- 状态: `protocol_ready_evidence_pending`
- 生产 profile: `inactive`; 生产权重冻结: `yes`
- 有效 real-agent 配对: `9`
- AI Jury 有效覆盖: `1` 个模型家族 / `1` 个 provider
- Sealed promotion: `0` tasks / `0` repositories / `0` sequences; 批准: `no`

当前阻塞:

1. Need order-swapped pairwise votes from at least three genuinely distinct model families and providers.
2. Need usable train labels across at least three training repositories plus repository-held-out development observations; both the cross-repository and adaptive label-seeking pairs had unchanged outcomes and create no new labels.
3. Need a repository-held-out candidate profile trained without safety-gate leakage.
4. Need paired real-agent frozen-baseline/candidate shadow runs bound to that profile.
5. Need at least 50 sealed tasks across three repositories, ten sequences, and two unseen agent models with complete paired cost data.
6. Need a passing explicit promotion decision before any atomic activation can be considered.
<!-- MEMORYOS:READINESS:END -->

MemoryOS V2.3 是面向编码 Agent 的本地优先 Reality Intelligence 层。它在 V2.2 的不可变证据、双时态 Current Truth、Git-aware freshness、检索硬化和真实工作负载协议之上，新增可回退的 Minimum Sufficient Context 编译层。MCP、HTTP、CLI 和 React Workbench 继续共享同一 SQLite 事实源。

## DeepSeek Harness 实测快照

当前 `dsh-memoryos 0.2.0` 已拆分为独立的 DeepSeek Harness（DSH）Bundle。它不是“零成本提示词”：启用后会增加工具 Schema 和取回内容，价值必须由与 `no_memory` 同题、同模型、同预算的对照来判断。用户现在可以直接对 Agent 说“关闭 OS”“开启 OS”或询问状态；开关会真实装卸记忆工具并跨重启保存。

| 验证面 | 冻结结果 | 可以得出的结论 |
|---|---:|---|
| 插件全功能验收 | 14/14 隐藏验收通过；13/14 严格协议通过 | 安装、开关、Full、Progressive、Explain、Delta、计量、缓存与隔离链路可用 |
| 自然语言持久开关 | 0.2.0 tarball 断网安装后契约与真实 Loader/HMR 27/27 通过 | 普通关闭只留控制工具；健康检查成功后恢复；严格 `no_memory` 保持零 Schema |
| 中等编程任务 | 单题中 A/C 均通过且 C 少 16.20% 输入；另有 held-out 任务 A/B/C 均失败 | 有效率与定向信号，但尚无跨题成功率提升证据 |
| 跨 Session 记忆 | 三案例严格门 2/3；12 个新会话的回忆/基线/错 scope 隔离均符合预期 | 写入、硬重启回忆和 scope 隔离成立；一个源写入门仍受跨语言词法评分影响 |
| 记忆更新 | PASS：PostgreSQL 17 被 18 正确 supersede | 新 Current Truth 不会与旧版本同时作为当前事实返回 |
| 上下文淘汰 A/B | PASS：无记忆回答“不知道”，MemoryOS 恢复 `Glacier-47` | 原始对话确已被挤出活动上下文后，长期记忆仍可恢复 |

最后两项 live-r4 共 24 次 Provider 尝试、0 重试；输入 214,165、输出 3,743、推理 2,206 Token。三个写入会话分别记录 `write_tool_schema_tokens / memory_write_visible_tokens / provider_input_tokens`，总计 `1,794 / 7,779 / 103,687`。MemoryOS 两项是 `unicode-heuristic-v1` 组件估算，Provider 输入是供应商精确值。完整口径、事故记录和不可外推范围见[实测总览](docs/DEEPSEEK_HARNESS_EVIDENCE.md)。

当前源码版本：`2.3.0`。MSC 的语义、配置、证据和限制见 [V2.3 最小充分上下文](docs/MINIMUM_SUFFICIENT_CONTEXT.md)。当前 confirmatory real-agent 证据不足，所以默认 compiler 仍是 `legacy`、`effect_claim=none`；没有自动激活 MSC 或 learned retrieval profile 的路径。合并后应在干净 `main` 上重建发行包并运行：

```powershell
.\.venv\Scripts\python.exe scripts\main_release_smoke.py --distribution .\release\MemoryOS
```

V2.1 的历史验收映射和不可变机器报告继续保留在 [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) 与 `docs/verification/v2.1/`；V2.2 历史证据位于 `docs/verification/v2.2/`；V2.3 Golden 和 dry-run 工件位于 `docs/verification/v2.3/`。

## V2.3 能力

- 三档 Context Compiler 模式：字节兼容 `legacy`、只存诊断的 `msc_shadow` 和瘦生产响应 `msc`；证据门禁通过前默认保持 legacy。
- `budget` 仍是旧字符预算；新增带 exact/estimated 来源的 Token Counter、384/768/1536/3072 Profile、AUTO 策略和完整 payload 预算。
- 确定性 INDEX/FACT Atom、按需 EVIDENCE/HISTORY，以及覆盖极性、限定词、Truth/Freshness、有效时间和证据指针的 Atom hash。
- Pinned Constraint 和 Contested Bundle 原子安全下限；精确去重只合并完全等价事实并保留所有证据，语义去重仍是 Shadow-only。
- 兼容扩展 `memory_explain` 的 expected hash/sections/Token 预算，以及会在变更时失效的一次证据展开。
- 显式 `previous_context_id` Delta、Scope/Policy/Tokenizer/TTL/完整性校验、低效 Delta 的 Full Rebase 和不进长期备份的可丢弃 Snapshot 缓存。
- 启动时固定的 `all/core/context/governance/debug` MCP Profile，工具顺序与 Schema hash 确定；`context` 仅暴露 `memory_context`，用于降低只读 Agent 每轮重复发送的工具 Schema；服务端 Schema 减少不被冒充为 Provider Token 节省。
- 独立 Context Efficiency Study 分开 Provider input/output、缓存、成本、延迟、记忆交付/证据/历史/Delta、Schema、安全和最差组，并固定 0.5/0.65/0.8/0.9 Delta 阈值敏感性；dry run 可复现但不授权默认激活。
- [Context Efficiency 实执行器](benchmarks/context_efficiency/README.md) 可用同一入口运行五个冻结条件、本地 Qwen OpenAI-compatible Agent 或 DeepSeek Harness、cold/warm 配对、真实代码修改/测试与逐请求 usage；fixture 只验证执行契约，不作为模型收益证据。

- 保留 V1 的五级 scope、六类 memory、candidate-first 生命周期、来源、审计、TTL、逻辑忘却、备份和 7 个 MCP 工具。
- 从 evidence span 生成标准化 Claim；实体别名只在同 scope/type 内解析，可审计合并与 redirect。
- Claim 关系支持 equivalent/supports/contradicts/supersedes 等；Current Truth 返回 `resolved | contested | stale | unknown`。
- ClaimIdentity 与只追加 ClaimVersion 分离；`transaction_from/to` 和 `valid_from/to` 支持按“当时已知”重建历史，不用当前行猜测过去。
- 明确冲突由规则处理；只有不确定 claim pair 可进入 bounded model judge。判断、弃权、失败、provider fingerprint、prompt version 与 evidence hash 都进入 Possible Conflict 审计队列。
- Source Anchor 使用 Tree-sitter 解析 Python、TypeScript、JavaScript、Rust 的相关 symbol；其他语言使用 bounded snippet/context hash。
- Git freshness 状态机识别 `fresh / moved / suspect / stale / unknown`，lazy + HEAD cache；refresh 只产生 replacement candidate，不改写原记忆。
- Retrieval 2.0 将 candidate retrieval、fusion、governance scoring、rerank 与 diversity 拆成显式阶段。生产继续使用冻结的 FTS/vector/graph/temporal 基线；显式 Shadow 可从 allowlist 选择查询配方，并为精确代码查询增加结构化 Source Anchor 通道。请求、实际执行、降级通道、阶段耗时及分数契约全部持久化。
- sqlite-vec 按 provider/model/dimension 建立持久化实时 namespace，支持 doctor、状态和重建；Exact NumPy 是明确降级路径，扩展缺失不会阻止启动。
- Context Compiler 按 task intent、coverage、truth/freshness、utility/cost 和预算选择最小证据集；未决冲突强制呈现双方。
- Grounded consolidation 校验 supporting/counter memory IDs 与独立来源；离线 extractive fallback 明确标注。所有抽象与 distillation 只生成 candidate，永不自动激活。
- Memory Health 用可解释分数管理 Hot/Warm/Cold/Archived；归档可逆，唯一 accepted current truth 不可归档，Cold/Archived 才能参与 distillation。
- helpful/unhelpful feedback 可审计，只影响 retrieval utility，不修改事实状态。
- 12 个 stdio MCP 工具的兼容 `all` Profile、V2.3 HTTP API/CLI，以及包含 Current Truth 版本、Possible Conflicts、Memory Health 与向量诊断的 Workbench。
- CodingMemoryBench Fixture Regression 分离 runtime input 与 gold scorer，包含 hard negatives、时间和冲突三模式对照，并对满分给出过拟合警告；另有独立 production-path integration suite，二者均不声明真实 Agent 效果。
- 实测 100,000 记录 FTS-first RetrievalPipeline + ContextCompiler P95；该 Tier 1 fixture 未执行 embedding、Claim/Relation 或模型通道，也不声明模型收益。

## 从源码运行（Windows PowerShell）

需要 Python 3.12、Node.js 20.19+ 和 pnpm 11。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

Set-Location web
pnpm install --frozen-lockfile
pnpm build
Set-Location ..

.\.venv\Scripts\python.exe -m memoryos --data-dir .\data serve --no-open
```

Tree-sitter language pack 是 V2 core dependency。若要启用可选 SQLite ANN：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ann]"
```

未传 `--data-dir` 时，Windows 默认数据目录为 `%LOCALAPPDATA%\MemoryOS`，也可用 `MEMORYOS_HOME` 覆盖。HTTP 仅绑定 loopback；浏览器获得 HttpOnly 同源写 cookie，外部写客户端使用 `<data-dir>\auth.token`。

## Windows 发行包

```powershell
.\release\MemoryOS\MemoryOS.exe --data-dir .\memoryos-data serve
```

发行形式为 PyInstaller `onedir`，必须保留整个 `release\MemoryOS` 目录。V2.3 生产 smoke 会从真实 `0001_initial` 数据库启动，验证自动迁移到 `0005_context_efficiency`、旧数据与不可变 anchor 基线保留、all/core/context/governance/debug 五个确定性 MCP Profile、HTTP/UI/CLI、fixture benchmark 资源、sqlite-vec runtime 和重启持久化。当前已有的 V2.2 包是历史证据；合并后的 clean-main V2.3 包复验完成前不声称新二进制已发布。

## CLI 示例

```powershell
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data status --json
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data propose --repo my-repo --title "Use FastAPI" --content "Use FastAPI for the local API."
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data current-truth --query "backend framework"
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data debug-context "current backend constraints" --repo my-repo
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data consolidate --scope-key my-repo
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data vector-rebuild
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data refresh <memory-id> --repository-path C:\path\to\repo
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data backup --output .\backup.zip
```

运行 `python -m memoryos --help` 查看完整命令。

## MemoryBench 与验收

### Retrieval calibration dataset

`benchmarks/calibration_v1` contains the first versioned, data-backed retrieval calibration input:
300 Git-derived silver queries across six query repositories and five languages, plus a dedicated
seventh repository for cross-scope guards. Train/dev/test are held out by query repository. Runtime
queries and scorer-only qrels are separate, every artifact is SHA-256 pinned, and every query has an
exact-path positive, a future-history guard, and a cross-scope guard.

```powershell
.\.venv\Scripts\python.exe scripts\build_calibration_dataset.py
.\.venv\Scripts\python.exe scripts\validate_calibration_dataset.py
```

The dataset calibrates retrieval only. Its Git path-overlap labels are explicitly `silver`, not human
gold, and cannot justify truth-conflict confidence or memory-health thresholds. Protocol, sources,
limitations, and offline rebuild instructions are in
[`benchmarks/calibration_v1/README.md`](benchmarks/calibration_v1/README.md).

### Blind human review pilot (optional diagnostic)

`benchmarks/human_review_v1` adds the next anti-overfitting layer without manufacturing labels. It
contains 61 blinded cases and 1,922 candidate decisions per reviewer: 60 time-stratified train/dev
queries across five non-test repositories plus one public real-workload diagnostic. The existing
test repository remains sealed. Two assignments contain the same cases in different candidate
orders and omit silver qrels, target commits, workload expectations, confidence, and importance.

```powershell
.\.venv\Scripts\python.exe scripts\build_human_review_pack.py
.\.venv\Scripts\python.exe scripts\validate_human_review_pack.py
```

The pack is deliberately `pending_human_adjudication`, not gold. Turning this particular pack into
human gold would still require two completed human reviews and a separate human adjudicator, but
human annotation is no longer a production-calibration prerequisite. Its machine-readable coupling
audit also reports that the initial real task shares the MarkupSafe repository with the Git source
set. See
[`benchmarks/human_review_v1/README.md`](benchmarks/human_review_v1/README.md).

A separate, checked-in model-only exercise now covers both blind assignments and all 1,922 pairs.
The third model role adjudicated 527 core disagreements; relevance agreement was 75.70% but Cohen's
kappa was only 0.203, while safety agreement was 97.66% (kappa 0.834). These are provisional rubric
diagnostics, not human labels and not a production-weight approval. The complete incident log,
hashes, decisions, post-hoc silver comparison, and validation command are in
[`benchmarks/human_review_v1/model_review/README.md`](benchmarks/human_review_v1/model_review/README.md).

### AI-only executable calibration

`benchmarks/ai_calibration_v1` defines the active no-human route for replacing heuristic retrieval
weights. At least three distinct model families from three providers make order-swapped pairwise
judgments; runtime/model/prompt/response identities are hash-bound, and those votes
are uncertainty-weighted weak supervision, never truth. Selected memories then receive real coding
agent full/minus ablations. A constrained pairwise learner can only create a candidate profile, and
a separate sealed gate requires at least 50 tasks, three repositories, ten sequences, two unseen
agent models, a positive success lower confidence bound, no safety or worst-repository regression,
bounded latency/cost, and complete paired cost accounting. No stage automatically activates a
profile. Candidate profiles run only through an explicit paired shadow runner; the normal service
keeps the frozen production scorer. Training rejects sealed test/promotion observations rather than
printing their metrics during model selection. It also rejects duplicate observation IDs, requires
both AI-jury and real executable evidence inside the train partition, and binds the exact canonical
train/dev input SHA-256 into the candidate profile.

```powershell
.\.venv\Scripts\python.exe scripts\validate_ai_calibration.py
.\.venv\Scripts\python.exe scripts\run_executable_ablation.py --help
.\.venv\Scripts\python.exe scripts\run_weight_shadow.py --help
.\.venv\Scripts\python.exe scripts\build_retrieval_routing_shadow.py --help
.\.venv\Scripts\python.exe scripts\run_routing_shadow.py --help
.\.venv\Scripts\python.exe scripts\analyze_routing_shadow.py --help
.\.venv\Scripts\python.exe scripts\ai_calibration.py --help
```

The checked-in readiness registry currently says `protocol_ready_evidence_pending`: nine valid
real-agent full/minus pairs now cover six SWE-bench Verified tasks across Requests, Pylint, pytest,
and Seaborn. Only one Requests pair is discordant and creates a real TRAIN label; the other Requests
repeat, three cross-repository pairs, and four later label-seeking pairs preserve unchanged outcomes
rather than selecting only favorable examples. The model review still represents only one effective
model family/provider, training still lacks usable labels across three training repositories and the
required repository-held-out development observation, and there are no sealed promotion tasks.
Production weights therefore remain frozen. Protocol, evidence hashes, commands, and blockers are in
[`benchmarks/ai_calibration_v1/README.md`](benchmarks/ai_calibration_v1/README.md).

单独运行 V2 回归与 V2.1 盲测：

```powershell
.\.venv\Scripts\python.exe scripts\memorybench_v2.py
.\.venv\Scripts\python.exe scripts\coding_memory_bench.py
.\.venv\Scripts\python.exe scripts\benchmark_v21_pipeline.py
.\.venv\Scripts\python.exe scripts\agent_ab_v21.py --tasks 50
```

输出：

- `docs/verification/v2/memorybench-report.json`
- `docs/verification/v2/memorybench-report.html`
- `docs/verification/v2/acceptance-summary.json`
- `docs/verification/v2/verify-summary.json`
- `docs/verification/v2.1/coding-memory-bench.{json,html}`
- `docs/verification/v2.1/full-pipeline-performance.json`
- `docs/verification/v2.1/agent-ab.json`
- `docs/verification/v2.1/acceptance-summary.json`
- `docs/verification/v2.1/main-release-smoke.json`

真实模型与 fixture 严格分开：50-task fixture 只验证 paired harness、指标和 bootstrap 95% CI。由于当前环境未配置真实 coding-agent endpoint，real-model Agent A/B 被如实记录为 `external_blocker`、`effect_claim=none`；项目不声称真实模型准确率或效果提升。

### V2.2 真实仓库回放框架

开发分支新增仓库级三组回放：`no_memory / flat_memory / memoryos` 使用相同历史提交、相同提示和相同代理镜像；MemoryOS 组必须产生真实 MCP 审计与 `RetrievalRunRow`。代理只看到 base 及祖先，记忆数据库位于独立 sidecar，隐藏测试在 `--network none` 的固定镜像中运行。公开 smoke 使用 MarkupSafe 的固定历史提交、任务发布时间和许可证来源，但内置代理明确标为 `deterministic_fixture`；只有 `real_coding_agent` 才可能通过确认性门禁，因此该报告始终 `effect_claim=none`。

协议、威胁模型、确认性门槛和运行命令见 [V2.2 real-workload evaluation](docs/REAL_WORKLOAD_EVALUATION_V2_2.md)。

首次运行浏览器测试前安装 Chromium：

```powershell
Set-Location web
pnpm exec playwright install chromium
Set-Location ..
```

`scripts/verify_v21.py` 依次执行 19 个 fail-fast 门禁：后端质量/测试、V2 回归、V2.1 盲测、50 对 agent 协议或 blocker、100K FTS-first Core Pipeline 性能、前端质量/E2E、wheel、Windows package、V1→V2.1 production smoke、干净 main release smoke 和 A33–A52 manifest。任何一步失败即非零退出。

## 数据和隐私边界

- `memoryos.db`：SQLite WAL/FTS5 主事实库。
- `auth.token`：本地写操作及会记录检索/到期状态的 API token。
- `runtime.json`：最近一次服务地址。
- `logs/memoryos.log`：脱敏轮转日志。
- `backups/`：格式 3 版本化备份，包含 claim versions、possible conflicts 与 health；V2.1 可导入旧格式，导入前校验 entry/哈希/大小/记录数/完整 schema，隔离迁移通过后才原子替换；恢复与导入后 ANN 缓存会安全重建。

MemoryOS 不做全仓源码收藏或云同步。Source Anchor 只读取被明确引用的相关文件，保存 bounded excerpt/hash/symbol metadata；Git compare 只检查 anchor commit 到 HEAD 的相关路径。默认 provider 关闭，不记录完整 prompt。

## 文档

- [架构](ARCHITECTURE.md)
- [V2.3 最小充分上下文](docs/MINIMUM_SUFFICIENT_CONTEXT.md)
- [安全模型](SECURITY.md)
- [MCP 接入](MCP_SETUP.md)
- [验收证据](docs/ACCEPTANCE.md)
- [项目状态](PROJECT_STATUS.md)
- [开发问题总复盘](docs/DEVELOPMENT_PROBLEMS_RETROSPECTIVE.md)
- [实施决策](DECISIONS.md)
- [变更日志](CHANGELOG.md)
- [MemoryBench](benchmarks/memorybench_v2/README.md)
- [V2.1 Reality Intelligence](docs/REALITY_INTELLIGENCE_V2_1.md)
- [V2.2 real-workload evaluation](docs/REAL_WORKLOAD_EVALUATION_V2_2.md)
- [V2.2 performance tiers](docs/PERFORMANCE_TIERS_V2_2.md)
