# MemoryOS

MemoryOS V2.1 是面向编码 Agent 的本地优先 Reality Intelligence 层。它在 V2 的 Claim Graph、Git-aware freshness 与任务上下文之上，加入不可变 ClaimVersion 事务历史、真正双时态 Current Truth、确定性/不确定性冲突路由、持久化 sqlite-vec 路径、记忆健康治理和有来源约束的抽象巩固。MCP、HTTP、CLI 和 React Workbench 继续共享同一 SQLite 事实源。

当前版本：`2.1.0`。在干净 `main` 上的唯一全量验收入口为：

```powershell
.\.venv\Scripts\python.exe scripts\verify_v21.py
```

验收映射见 [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)，V2.1 机器报告位于 `docs/verification/v2.1/`。

## V2.1 能力

- 保留 V1 的五级 scope、六类 memory、candidate-first 生命周期、来源、审计、TTL、逻辑忘却、备份和 7 个 MCP 工具。
- 从 evidence span 生成标准化 Claim；实体别名只在同 scope/type 内解析，可审计合并与 redirect。
- Claim 关系支持 equivalent/supports/contradicts/supersedes 等；Current Truth 返回 `resolved | contested | stale | unknown`。
- ClaimIdentity 与只追加 ClaimVersion 分离；`transaction_from/to` 和 `valid_from/to` 支持按“当时已知”重建历史，不用当前行猜测过去。
- 明确冲突由规则处理；只有不确定 claim pair 可进入 bounded model judge。判断、弃权、失败、provider fingerprint、prompt version 与 evidence hash 都进入 Possible Conflict 审计队列。
- Source Anchor 使用 Tree-sitter 解析 Python、TypeScript、JavaScript、Rust 的相关 symbol；其他语言使用 bounded snippet/context hash。
- Git freshness 状态机识别 `fresh / moved / suspect / stale / unknown`，lazy + HEAD cache；refresh 只产生 replacement candidate，不改写原记忆。
- Retrieval 2.0 使用 FTS/vector/graph/temporal candidate union、RRF、freshness/scope/evidence filter、可选 top-N reranker 和 MMR，并持久化逐项 trace。
- sqlite-vec 按 provider/model/dimension 建立持久化实时 namespace，支持 doctor、状态和重建；Exact NumPy 是明确降级路径，扩展缺失不会阻止启动。
- Context Compiler 按 task intent、coverage、truth/freshness、utility/cost 和预算选择最小证据集；未决冲突强制呈现双方。
- Grounded consolidation 校验 supporting/counter memory IDs 与独立来源；离线 extractive fallback 明确标注。所有抽象与 distillation 只生成 candidate，永不自动激活。
- Memory Health 用可解释分数管理 Hot/Warm/Cold/Archived；归档可逆，唯一 accepted current truth 不可归档，Cold/Archived 才能参与 distillation。
- helpful/unhelpful feedback 可审计，只影响 retrieval utility，不修改事实状态。
- 12 个 stdio MCP 工具、V2.1 HTTP API/CLI，以及包含 Current Truth 版本、Possible Conflicts、Memory Health 与向量诊断的 Workbench。
- Blind CodingMemoryBench 分离 runtime input 与 gold scorer，包含 hard negatives、时间和冲突三模式对照，并对满分给出过拟合警告。
- 实测 100,000 记录完整 RetrievalPipeline + ContextCompiler P95；真实 coding-agent endpoint 缺失时只生成明确 blocker，不声明模型收益。

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

发行形式为 PyInstaller `onedir`，必须保留整个 `release\MemoryOS` 目录。生产 smoke 会从真实 `0001_initial` 数据库启动，验证自动迁移到 `0003_reality_intelligence_hardening`、旧数据保留、12 个 MCP 工具、HTTP/UI/CLI、两套 benchmark 资源、sqlite-vec runtime 和重启持久化。

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
.\.venv\Scripts\python.exe scripts\ai_calibration.py --help
```

The checked-in readiness registry currently says `protocol_ready_evidence_pending`: two valid
real-agent full/minus pairs now cover one SWE-bench Verified task, with one discordant real TRAIN
label and a repeated latency reduction. The model review still represents only one effective model
family/provider, training still lacks the required repository-held-out development evidence, and
there are no sealed promotion tasks. Production weights therefore remain frozen. Protocol,
evidence hashes, commands, and exact blockers are in
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

`scripts/verify_v21.py` 依次执行 19 个 fail-fast 门禁：后端质量/测试、V2 回归、V2.1 盲测、50 对 agent 协议或 blocker、100k 全管线性能、前端质量/E2E、wheel、Windows package、V1→V2.1 production smoke、干净 main release smoke 和 A33–A52 manifest。任何一步失败即非零退出。

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
- [实施决策](DECISIONS.md)
- [变更日志](CHANGELOG.md)
- [MemoryBench](benchmarks/memorybench_v2/README.md)
- [V2.1 Reality Intelligence](docs/REALITY_INTELLIGENCE_V2_1.md)
- [V2.2 real-workload evaluation](docs/REAL_WORKLOAD_EVALUATION_V2_2.md)
