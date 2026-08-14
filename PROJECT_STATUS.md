# MemoryOS 项目状态

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

## 当前结论

- 版本：V2.3.0
- 状态：**V2.3 Minimum Sufficient Context 源码实现与确定性 dry run 已完成；真实 Agent confirmatory 证据与合并后 clean-main 发行复验待完成，默认保持 legacy**
- 日期：2026-08-15（Asia/Shanghai）
- 干净 V2 基线：commit `b0cae26dfab0141876ceffa1fde97cc5e2b92591`，dirty=false，16/16 PASS
- V2.1 合并提交：`eaf10ba700455513f4eb4a392f4c042a6b4ea125`
- 最终全链验证基线：commit `b630496a91fb4188fbbd154aa33d1a4ccfd91da5`，dirty=false，19/19 release gates PASS
- 最终 A52 clean-main 基线：commit `9f2e2e7909c547b4a6b19d3e7a1ef40031d5ebe6`，dirty=false，package smoke PASS
- V2.1 历史验收入口：`.\.venv\Scripts\python.exe scripts\verify_v21.py`
- V2.1 历史验收范围：V1 A01–A14、V2 A15–A32 回归，V2.1 A33–A52
- V2.3 MSC（2026-08-15）：已实现 legacy/msc_shadow/msc 三模式、完整 payload Token 预算、Context Atom/确定性去重、Pinned/Contested 原子安全下限、按需 Explain、显式 Delta/Full Rebase、可丢弃 Snapshot、all/core/governance/debug 固定 MCP Profile 与独立 Context Efficiency Study。Study 分开 Provider input/output/cached、成本、延迟、delivery/evidence/history/delta/full-equivalent、Schema 和安全/行为/系统指标，并哈希绑定 0.5/0.65/0.8/0.9 阈值矩阵。V2.2 Golden 基线和 V2.3 dry run 均可重建。Dry run 为 deterministic fixture，无 Provider Usage，`effect_claim=none`；预注册 2pp 非劣界限在 10% 不一致假设下约需 1,546 个独立配对任务，因此未切换默认。
- V2.3 本地分支验证（2026-08-15）：V2.3 专项 36 passed；Ruff/format PASS，Mypy 检查 115 个源码文件 0 issues，后端全量 332 passed/0 failed（仅 2 条已知上游 warning）；前端 typecheck/ESLint PASS，Vitest 4 files/11 tests PASS，Vite 生产构建 1,745 modules PASS。这是源码回归证据，不代替 clean-main 打包 smoke 或 real-agent confirmatory evidence。
- V2.2 开发状态（2026-08-11）：仓库级三组真实 workload 框架已实现；固定 MarkupSafe 历史提交的 1-task/3-condition 公共 smoke 协议有效、隐藏测试全过、MemoryOS 产生真实 retrieval run。任务发布时间、跨项目来源仓库、agent commit 补丁、宿主 Git 控制面及 `real_coding_agent` 证据门禁均已机器校验；全仓后端回归 142 passed，确定性 fixture 仍明确 `effect_claim=none`
- Retrieval calibration（2026-08-12）：首版 `memoryos-git-silver-v1` 已从 7 个固定公开仓库生成，包含 6 个查询仓库、5 种语言、300 queries、3,656 candidates、9,600 judgments；train/dev/test=200/50/50，manifest digest `52e670691d4c723680f7d2c67efcce31701001c88218bd3d915c82de5013eb3a`。标签明确为 Git path-overlap silver，不冒充人工 gold。
- Human review pilot（2026-08-12）：`memoryos-human-review-v1-pilot` 已生成 61 个盲标案例、每位评审 1,922 个候选判断，双份候选顺序独立、test 封存、构建不读 qrels，并包含来源映射与过耦合审计；manifest digest `ecf532c8ebbe7b3f9866623eab0e9fb53cd979abe486fb29359cb9ca7f20729f`。当前状态严格为 `pilot_unlabeled`，双人独立标注、仲裁、外部仓库 holdout 和真实 Agent shadow evidence 尚未完成。
- Model-only blind review（2026-08-12）：两名有效隔离子 Agent 完成 1,922×2 判断，第三方角色逐条仲裁 527 个核心分歧，最终 provisional artifact SHA-256 `0c836306283ae750521b5526b844c8b0a0ef6c0a05137bcb5f846dcd558e83e1`。相关性 raw agreement 75.70%，但 Cohen's κ 仅 0.203；安全判断 agreement 97.66%、κ 0.834。原 reviewer-A 轮次因自报越界代码搜索被整轮作废并替换。该结果仅用于 rubric/主动学习诊断，不改变 `pilot_unlabeled`，不批准生产权重。
- AI-only executable calibration（2026-08-12～13）：已冻结 `memoryos-ai-executable-calibration-v1` 协议，实现三 provider/三模型家族双顺序 AI Jury、运行时/提示/响应哈希绑定、概率弱监督、真实 full/minus-memory 消融、符号约束 pairwise 权重训练、dev 数据选择正则强度、显式 shadow scoring、仓库 holdout 和 sealed promotion gate；训练命令拒绝 test、fixture 与重复 observation，要求 AI Jury + real executable 两种标签都位于 train 分区，并把完整 train/dev 输入哈希绑定到候选 profile；晋级 CI 按 task 聚类且要求完整 task×agent 矩阵。当前 readiness 为 9 个协议有效 real-agent pair、6 个 SWE-bench Verified task、1 个有效 jury provider/model family、0 个 sealed promotion task。只有 Requests 的 1 个 pair 为 full 成功/minus 失败并生成真实 TRAIN label；另一个 Requests repeat、3 个 cross-repository pair 和 4 个后续 label-seeking pair 均为双臂同结果。证据仍不足以覆盖 3 个 train repository 和 held-out dev observation，生产权重保持冻结，且不存在自动激活路径。
- Public BGE retrieval bootstrap（2026-08-13）：SWE-Gym 1,942 条可用查询、仓库级 train/dev/test 和真实 `BAAI/bge-small-en-v1.5` 产生 19.25% FTS / 80.75% vector 的非生产先验；仅投影 FTS/vector 相对 RRF 比例，graph/temporal/K/Lexical MMR 及安全门全部冻结。真实 MemoryOS 同候选池 52-query 回放的仓库宏平均 NDCG@10 为 0.49611→0.51330、required Recall@5 为 0.11538→0.17308，但 NDCG 95% CI 跨零，且 Pandas 两指标回退。机器门禁结论为 `retain_frozen_baseline`；一组 pytest 真实 Agent full/minus-memory 显示记忆有帮助，但不识别该权重比例。生产权重未变。
- Query-adaptive retrieval（2026-08-13）：已实现声明式 `RetrievalPlan` 候选架构和 router v2。执行链拆为 candidate/fusion/governance/rerank/diversity；精确查询可用独立 Source Anchor 通道；请求与实际通道能力、降级、reranker、融合参数、阶段耗时和 bounded score contract 均进入证据。路由决策只使用离散信号和原因码，不再生成或阈值化伪概率。聚合器按 task 而非重复运行 bootstrap，并设 worst repository/agent/recipe、安全、成本和时延门禁。该能力只能显式 Shadow 启用；默认生产仍执行冻结 safe-hybrid，当前没有真实 Agent 因果数据支持激活。
- Routing hardening 本地验证（2026-08-13）：全仓 Ruff/format PASS，`memoryos` 106 个源码文件 Mypy 0 error；后端全量 262 passed、0 failed（1 条上游 Starlette/httpx 弃用 warning）。这是实现回归证据，不是路由效果证据。
- V2.2 性能分层（2026-08-14）：Tier 1 明确更名为 100K FTS-first Core Pipeline，仅 FTS 实际贡献，search P50/P95 17.715/20.598 ms、context 239.949/263.738 ms。Tier 2 使用真实本地 `BAAI/bge-small-en-v1.5`（384 维）完整实测 10K/20K：分别含等量 embeddings/claims 与 100/200 relations，FTS/vector/Claim-Relation/temporal 全部实际贡献；sqlite-vec ANN search P50/P95 分别为 278.454/313.424 和 322.170/375.527 ms，强制 exact fallback 分别为 1679.675/1733.581 和 3054.057/3165.333 ms。Tier 2 仅为 manual/scheduled synthetic performance evidence，不冒充 CI 或 Agent 效果；Tier 3 因没有兼容 reranker/relationship endpoint 未执行。
- Current Truth 批量化（2026-08-14）：同机、同一 1/10/1000 identity 合成语料的前后对照中，1000 identities 从 7,046 条 SQL、P50/P95 1217.033/1311.148 ms 降到恒定 9 条 SQL、76.094/82.460 ms；完整原始结果保存在 `docs/verification/v2.2/current-truth-performance.json`，不外推为真实 Agent 效果。

MemoryOS V2.1 发布快照中的 A47 因当时未提供真实 coding-agent endpoint 与凭据而走 `external_blocker` 路径；该历史验收记录保持不变。2026-08-12 当前开发环境开始接入隔离的真实 Codex runtime，先取得 Requests 的 2 个 full/minus pair，随后扩展到上述 9 个协议有效 pair；它们仍不足以回写旧版 50-task 验收或作 confirmatory 产品效果声明。50-task fixture 仍只证明 harness/metrics/CI plumbing。

## V2.3 最小充分上下文交付范围

| 领域 | 已交付内容 | 状态 |
| --- | --- | --- |
| Compatibility | `budget` 永久保持字符语义；legacy 响应保留；MSC 为显式模式 | Complete |
| Token accounting | deterministic estimated counter、exact counter 注入、完整 payload 预算、Provider/记忆分账 | Complete; real Provider Usage pending |
| Atoms and safety | INDEX/FACT、Evidence/History 边界、Atom hash、Pinned/Contested bundle、exact dedup | Complete |
| Progressive disclosure | 兼容扩展 `memory_explain`，hash 失效、section 预算、多来源一次证据回溯 | Complete |
| Delta | 显式游标、Scope/Policy/Tokenizer/TTL/integrity 校验、可解释 Full Rebase | Complete |
| Migration/backup | `0005_context_efficiency`；Snapshot 是有界可丢弃缓存，不进长期备份，Restore 后安全 rebase | Complete |
| MCP profiles | deterministic `all/core/governance/debug`；四个 Schema Snapshot/hash | Complete |
| Evaluation | 独立五条件 Study、完整分账、paired bootstrap、阈值矩阵、power、safety/transparency/worst-group gates | Dry run complete; confirmatory pending |
| Activation | 默认 `legacy`，显式 `msc_shadow`/`msc` 可用，无自动激活 | Evidence gate retained |

## V2.2 交付范围

| 领域 | 已交付内容 | 状态 |
| --- | --- | --- |
| Migration | 0001/0002 显式 immutable operations；0003 回填版本；0004 分离 immutable anchor baseline/observation 并可 downgrade/replay | Complete |
| Bitemporal Truth | ClaimIdentity + append-only ClaimVersion，valid/transaction 双时间与 reason/actor | Complete |
| Conflict 2.0 | deterministic uncertain router、bounded model、Possible Conflict 审计/人工处理、abstain safety | Complete |
| ANN | sqlite-vec 持久化 namespace、实时 upsert/search、doctor/status/rebuild、exact fallback | Complete |
| CodingMemoryBench fixture regression | input/gold 隔离、hard negatives、baseline/V2/V2+model、满分警告；与 production-path integration 分开 | Complete |
| Retrieval calibration | 300-query public Git silver set、仓库级 holdout、future/cross-scope guards、可复现构建与哈希校验 | Silver baseline complete |
| Human retrieval review | 61-case 盲标包、双评顺序隔离、1,922 judgments/reviewer、test 封存、过耦合/留一来源计划 | Optional diagnostic; not active promotion path |
| Model review exercise | 双模型盲评、527 条第三方仲裁、全链哈希与银标后验诊断 | Provisional only; no weight promotion |
| AI-only weight calibration | 3+ 模型家族双顺序 Jury、真实单记忆消融、受约束学习、sealed 多 Agent 晋级门禁 | 9 valid real pairs / 6 tasks / 1 TRAIN label; fitting blocked by evidence gates; weights frozen |
| Public retrieval prior | SWE-Gym 仓库 holdout、真实 BGE、同候选池 RRF Shadow、分层 bootstrap 与最差仓库门禁 | 52-query diagnostic positive point estimate; gate failed; frozen baseline retained |
| Query-adaptive retrieval | allowlisted recipes、五阶段执行、Source Anchor exact channel、实际能力遥测、bounded score contract、task-level promotion gate | Candidate Shadow complete; production frozen; sealed causal matrix pending |
| Agent A/B | V2.1 ≥50 paired fixture；V2.2 已完成 Requests 真实模型 2 次 full/minus repeat；50+ confirmatory 样本与多模型仍未完成 | Exploratory real evidence only |
| Consolidation | 严格 support/counter 白名单、独立来源、provider/prompt、offline fallback、candidate-only | Complete |
| Memory Health | Hot/Warm/Cold/Archived、解释分数、可逆 archive、唯一 truth 保护、candidate distillation | Complete |
| Interfaces/UI | Current Truth 版本、Possible Conflicts、Memory Health、向量诊断、盲测 dashboard | Complete |
| Release | wheel、Windows onedir、0001→0004 packaged smoke；旧数据、12 MCP、Tree-sitter、sqlite-vec、HTTP/UI/CLI 与重启持久化 | V2.2 smoke PASS (10.170 s) |
| Main release | clean-main rebuild、0001→0004 packaged smoke 与 A52 | Pending V2.2 package rerun |

## V2.1 历史合并后实测快照

| 门禁 | 实测结果 |
| --- | --- |
| Backend import | `memoryos.__version__ == 2.1.0` |
| Ruff / format / Mypy | PASS / PASS / 73 source files 0 issues |
| Pytest | 62 passed；6 个上游/SQLite adapter warning |
| TypeScript / ESLint | 0 errors / 0 warnings |
| Vitest | 4 files，11 passed |
| Vite production build | 1,745 modules；JS 363.41 kB（gzip 108.10 kB） |
| Playwright | 9 applicable passed，7 intentional device-matrix skips |
| Accessibility | overview、Intelligence 与 V2.1 health/settings axe violations 0 |
| Fixture Regression Result | 100 retrieval + 100 temporal + 100 conflict；V2 Recall@5/temporal/conflict F1 = 1.0；perfect-score warning present；不外推为生产或 Agent 效果 |
| 100K FTS-first Core Pipeline | search P50/P95 87.853/100.822 ms；context P50/P95 127.422/138.808 ms；未执行 embedding/Claim/Relation 通道 |
| Agent evidence | `external_blocker`，requested 50/completed 0，fixture harness-only |
| AI calibration Requests seed | 2 pairs / 4 arms；1 helped、1 unchanged、0 harmed；平均 latency effect -170.10 s |
| AI calibration readiness | 9 valid pairs / 6 tasks；1 TRAIN label；1 provider/model family；0 sealed promotion tasks；生产权重未变 |
| Public RRF Shadow | 52 queries / 2 repos；NDCG@10 +0.01719（95% CI -0.00847～+0.04612）；Recall@5 +0.05769；Pandas 回退；生产门禁 FAIL |
| Backend wheel | `memoryos-2.1.0-py3-none-any.whl`，135,473 bytes |
| Windows executable | `MemoryOS.exe`，15,506,332 bytes |
| Package smoke | PASS；clean path、0001→0003、12 MCP、两套 benchmark、sqlite-vec、Tree-sitter、HTTP/UI/CLI、restart，9.295 s |
| A33–A52 | 20/20 PASS；A47 按任务书允许路径记录真实外部 blocker，未声明模型效果 |

Playwright 的 7 个 skip 是明确的设备矩阵不适用项：移动布局断言只在 mobile 运行，会修改共享 fixture 或执行桌面审计的流程只在 desktop 运行。所有适用用例均通过。

## 证据与产物

- V2 clean baseline：`docs/verification/v2.1/v2-clean-baseline.json`
- CodingMemoryBench fixture regression：`docs/verification/v2.1/coding-memory-bench.{json,html}`
- 100K FTS-first Core Pipeline（历史 V2.1 文件名保留）：`docs/verification/v2.1/full-pipeline-performance.json`
- real-agent/blocker：`docs/verification/v2.1/agent-ab.json`
- A33–A52：`docs/verification/v2.1/acceptance-summary.json`
- main smoke：`docs/verification/v2.1/main-release-smoke.json`
- 19-gate 总验证：`docs/verification/v2.1/verify-summary.json`
- package smoke：`docs/verification/package-smoke.json`
- V2.2 MarkupSafe 公共三组回放：`docs/verification/v2.2/markupsafe-public-smoke/{report,run-metadata}.json`
- V2.3 的 V2.2 Context Golden：`docs/verification/v2.3/v22-context-compiler-golden.json`
- V2.3 Context Efficiency deterministic dry run：`docs/verification/v2.3/context-efficiency-dry-run.json`
- AI calibration Requests 真实消融摘要：`benchmarks/ai_calibration_v1/evidence/requests-6028-real-agent-ablation-v1.json`
- Public BGE/RRF Shadow 摘要：`benchmarks/ai_calibration_v1/evidence/public-rrf-shadow-v1.json`
- wheel：`build/wheel/memoryos-2.1.0-py3-none-any.whl`
  - SHA-256 `C0280EC2A2AC5B79EA54C1CD6E2AE361E2255B3C97113C706A963867E1323033`
- Windows：`release/MemoryOS/MemoryOS.exe`
  - SHA-256 `B9330BDF6956F3A0BD9F41A80229DBD7FA561E9B430764A788C58C31B205F5FD`

## 已知边界

- MSC 当前只有实现/协议/安全回归证据；还没有满足预注册 power、完整 Provider Usage 和最差组门禁的真实 coding-agent confirmatory set。因此不声称 Token 降幅或任务成功改善，也不将 `msc` 设为默认。
- `unicode-heuristic-v1` 是确定性估算而非特定 Provider exact tokenizer；MCP Profile 的 Schema 估算也不是 Provider 实际 input token。
- 当前已有一个可放入隔离容器的真实 Codex runtime，但仍缺至少两个独立 provider/模型家族、短期凭据与 allowlisted gateway、完整 cost 计量、跨仓库 train/dev 证据，以及 50-task × 2 unseen-agent sealed promotion 矩阵；现有 Requests 结果只作探索性校准证据。
- Public RRF Shadow 只覆盖两个 public test 仓库，bootstrap 区间跨零且存在 Pandas 域回退；不能用其 19/81 比例替换生产 50/50 基线。下一轮需增加独立仓库，并直接做 frozen-baseline 与 candidate-weight 的成对 Agent Shadow，而不是重复同标签生成器的数据。
- router v2 已删除 query-time confidence 阈值和虚构概率，规则分类只输出原因码，但 recipe 选择仍是确定性候选；它解决执行拓扑和可观测性问题，没有证明“哪个 recipe 对真实 Agent 最好”。80/1000 pool、40 rerank window、冻结 RRF/Lexical MMR 等结构参数仍是版本化启发式基线。在跨仓库、跨模型成对 Shadow 与 sealed promotion 通过前，不得接入默认服务。
- Pytest 的 FastAPI/Starlette TestClient 与 Python 3.12 sqlite datetime adapter 发出弃用 warning；所有测试通过。
- PyInstaller 报告未安装可选 `tzdata`、`pysqlite2`、`MySQLdb`；MemoryOS 使用内置 SQLite，实际冻结包迁移与功能 smoke 已通过。
- 冻结 MCP 子进程有一条 `pydantic-settings` forward-reference warning；协议初始化、12 个工具和跨进程读写均通过。

产品边界仍是单机单用户、loopback-only、无云同步、无应用层静态加密、无全仓源码收藏。详见 `SECURITY.md`。
