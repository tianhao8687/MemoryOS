# MemoryOS

> [开发问题总复盘：开发中遇到的问题、失败实验、修复证据与遗留风险](docs/DEVELOPMENT_PROBLEMS_RETROSPECTIVE.md)
>
> [V2.2 Hardening Report：H-001～H-008 修复、迁移、测试、基准与限制](HARDENING_REPORT.md)

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

MemoryOS V2.2 是面向编码 Agent 的本地优先 Reality Intelligence 层。它在 V2.1 的不可变 ClaimVersion、双时态 Current Truth、Git-aware freshness 与任务上下文之上，进一步强化 Source Anchor 不可变性、真实产品路径评测、Claim/Relation 检索正确性、双时态查询扩展性和性能证据分层。MCP、HTTP、CLI 和 React Workbench 继续共享同一 SQLite 事实源。

当前版本：`2.2.0`。H-001～H-008 的实现、测试、基准和限制见 [V2.2 Hardening Report](HARDENING_REPORT.md)。合并后应在干净 `main` 上重建发行包并运行：

```powershell
.\.venv\Scripts\python.exe scripts\main_release_smoke.py --distribution .\release\MemoryOS
```

V2.1 的历史验收映射和不可变机器报告继续保留在 [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) 与 `docs/verification/v2.1/`；V2.2 证据位于 `docs/verification/v2.2/`。

## V2.2 能力

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
- 12 个 stdio MCP 工具、V2.2 HTTP API/CLI，以及包含 Current Truth 版本、Possible Conflicts、Memory Health 与向量诊断的 Workbench。
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

发行形式为 PyInstaller `onedir`，必须保留整个 `release\MemoryOS` 目录。生产 smoke 会从真实 `0001_initial` 数据库启动，验证自动迁移到 `0004_anchor_observation_hardening`、旧数据与不可变 anchor 基线保留、12 个 MCP 工具、HTTP/UI/CLI、fixture benchmark 资源、sqlite-vec runtime 和重启持久化。Production-path integration benchmark 作为源码验证证据单独保存在 `docs/verification/v2.2/`，不冒充打包内置功能。

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
