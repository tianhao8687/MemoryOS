# MemoryOS 项目状态

## 当前结论

- 版本：V2.1.0
- 状态：**V2.1 已合并 `main`，A33–A52 全部通过，发布验收完成**
- 日期：2026-08-10（Asia/Shanghai）
- 干净 V2 基线：commit `b0cae26dfab0141876ceffa1fde97cc5e2b92591`，dirty=false，16/16 PASS
- V2.1 合并提交：`eaf10ba700455513f4eb4a392f4c042a6b4ea125`
- 最终全链验证基线：commit `b630496a91fb4188fbbd154aa33d1a4ccfd91da5`，dirty=false，19/19 release gates PASS
- 最终 A52 clean-main 基线：commit `9f2e2e7909c547b4a6b19d3e7a1ef40031d5ebe6`，dirty=false，package smoke PASS
- 最终验收入口：`.\.venv\Scripts\python.exe scripts\verify_v21.py`
- 验收范围：V1 A01–A14、V2 A15–A32 回归，V2.1 A33–A52
- V2.2 开发状态（2026-08-11）：仓库级三组真实 workload 框架已实现；固定 MarkupSafe 历史提交的 1-task/3-condition 公共 smoke 协议有效、隐藏测试全过、MemoryOS 产生真实 retrieval run。任务发布时间、跨项目来源仓库、agent commit 补丁、宿主 Git 控制面及 `real_coding_agent` 证据门禁均已机器校验；全仓后端回归 142 passed，确定性 fixture 仍明确 `effect_claim=none`
- Retrieval calibration（2026-08-12）：首版 `memoryos-git-silver-v1` 已从 7 个固定公开仓库生成，包含 6 个查询仓库、5 种语言、300 queries、3,656 candidates、9,600 judgments；train/dev/test=200/50/50，manifest digest `52e670691d4c723680f7d2c67efcce31701001c88218bd3d915c82de5013eb3a`。标签明确为 Git path-overlap silver，不冒充人工 gold。
- Human review pilot（2026-08-12）：`memoryos-human-review-v1-pilot` 已生成 61 个盲标案例、每位评审 1,922 个候选判断，双份候选顺序独立、test 封存、构建不读 qrels，并包含来源映射与过耦合审计；manifest digest `ecf532c8ebbe7b3f9866623eab0e9fb53cd979abe486fb29359cb9ca7f20729f`。当前状态严格为 `pilot_unlabeled`，双人独立标注、仲裁、外部仓库 holdout 和真实 Agent shadow evidence 尚未完成。
- Model-only blind review（2026-08-12）：两名有效隔离子 Agent 完成 1,922×2 判断，第三方角色逐条仲裁 527 个核心分歧，最终 provisional artifact SHA-256 `0c836306283ae750521b5526b844c8b0a0ef6c0a05137bcb5f846dcd558e83e1`。相关性 raw agreement 75.70%，但 Cohen's κ 仅 0.203；安全判断 agreement 97.66%、κ 0.834。原 reviewer-A 轮次因自报越界代码搜索被整轮作废并替换。该结果仅用于 rubric/主动学习诊断，不改变 `pilot_unlabeled`，不批准生产权重。
- AI-only executable calibration（2026-08-12）：已冻结 `memoryos-ai-executable-calibration-v1` 协议，实现三 provider/三模型家族双顺序 AI Jury、运行时/提示/响应哈希绑定、概率弱监督、真实 full/minus-memory 消融、符号约束 pairwise 权重训练、dev 数据选择正则强度、显式 shadow scoring、仓库 holdout 和 sealed promotion gate；训练命令拒绝 test、fixture 与重复 observation，要求 AI Jury + real executable 两种标签都位于 train 分区，并把完整 train/dev 输入哈希绑定到候选 profile；晋级 CI 按 task 聚类且要求完整 task×agent 矩阵。新增 SWE-bench Verified `psf__requests-6028`：2 个协议有效 real-agent pair 中 1 个 full 成功/minus 失败并生成真实 TRAIN label，另 1 个双臂均成功；full 相对 minus 的平均耗时差为 -170.10 秒。readiness 现为真实消融对 2、有效 jury provider/模型家族 1、sealed promotion tasks 0；单仓库输入被训练器按最低 3 仓库规则拒绝，生产权重保持冻结，且不存在自动激活路径。
- Public BGE retrieval bootstrap（2026-08-13）：SWE-Gym 1,942 条可用查询、仓库级 train/dev/test 和真实 `BAAI/bge-small-en-v1.5` 产生 19.25% FTS / 80.75% vector 的非生产先验；仅投影 FTS/vector 相对 RRF 比例，graph/temporal/K/MMR 及安全门全部冻结。真实 MemoryOS 同候选池 52-query 回放的仓库宏平均 NDCG@10 为 0.49611→0.51330、required Recall@5 为 0.11538→0.17308，但 NDCG 95% CI 跨零，且 Pandas 两指标回退。机器门禁结论为 `retain_frozen_baseline`；一组 pytest 真实 Agent full/minus-memory 显示记忆有帮助，但不识别该权重比例。生产权重未变。

MemoryOS V2.1 发布快照中的 A47 因当时未提供真实 coding-agent endpoint 与凭据而走 `external_blocker` 路径；该历史验收记录保持不变。2026-08-12 当前开发环境已接入隔离的真实 Codex runtime 并取得上述 2 个 full/minus pair，但仍不足以回写旧版 50-task 验收或作 confirmatory 产品效果声明。50-task fixture 仍只证明 harness/metrics/CI plumbing。

## V2.1 交付范围

| 领域 | 已交付内容 | 状态 |
| --- | --- | --- |
| Migration | 0001/0002 显式 immutable operations；0003 回填版本，可 downgrade/replay | Complete |
| Bitemporal Truth | ClaimIdentity + append-only ClaimVersion，valid/transaction 双时间与 reason/actor | Complete |
| Conflict 2.0 | deterministic uncertain router、bounded model、Possible Conflict 审计/人工处理、abstain safety | Complete |
| ANN | sqlite-vec 持久化 namespace、实时 upsert/search、doctor/status/rebuild、exact fallback | Complete |
| CodingMemoryBench | input/gold 隔离、hard negatives、baseline/V2/V2+model、满分警告 | Complete |
| Retrieval calibration | 300-query public Git silver set、仓库级 holdout、future/cross-scope guards、可复现构建与哈希校验 | Silver baseline complete |
| Human retrieval review | 61-case 盲标包、双评顺序隔离、1,922 judgments/reviewer、test 封存、过耦合/留一来源计划 | Optional diagnostic; not active promotion path |
| Model review exercise | 双模型盲评、527 条第三方仲裁、全链哈希与银标后验诊断 | Provisional only; no weight promotion |
| AI-only weight calibration | 3+ 模型家族双顺序 Jury、真实单记忆消融、受约束学习、sealed 多 Agent 晋级门禁 | 2 real pairs / 1 TRAIN label; fitting blocked by evidence gates; weights frozen |
| Public retrieval prior | SWE-Gym 仓库 holdout、真实 BGE、同候选池 RRF Shadow、分层 bootstrap 与最差仓库门禁 | 52-query diagnostic positive point estimate; gate failed; frozen baseline retained |
| Agent A/B | V2.1 ≥50 paired fixture；V2.2 已完成 Requests 真实模型 2 次 full/minus repeat；50+ confirmatory 样本与多模型仍未完成 | Exploratory real evidence only |
| Consolidation | 严格 support/counter 白名单、独立来源、provider/prompt、offline fallback、candidate-only | Complete |
| Memory Health | Hot/Warm/Cold/Archived、解释分数、可逆 archive、唯一 truth 保护、candidate distillation | Complete |
| Interfaces/UI | Current Truth 版本、Possible Conflicts、Memory Health、向量诊断、盲测 dashboard | Complete |
| Release | wheel、Windows onedir、0001→0003 packaged smoke | Complete |
| Main release | clean-main rebuild、0001→0003 packaged smoke 与 A52 | Complete |

## 合并后实测快照

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
| Blind benchmark | 100 retrieval + 100 temporal + 100 conflict；V2 Recall@5/temporal/conflict F1 = 1.0；perfect-score warning present |
| 100K full pipeline | search P50/P95 87.853/100.822 ms；context P50/P95 127.422/138.808 ms |
| Agent evidence | `external_blocker`，requested 50/completed 0，fixture harness-only |
| AI calibration evidence | Requests real-agent 2 pairs / 4 arms；1 helped、1 unchanged、0 harmed；平均 latency effect -170.10 s；生产权重未变 |
| Public RRF Shadow | 52 queries / 2 repos；NDCG@10 +0.01719（95% CI -0.00847～+0.04612）；Recall@5 +0.05769；Pandas 回退；生产门禁 FAIL |
| Backend wheel | `memoryos-2.1.0-py3-none-any.whl`，135,473 bytes |
| Windows executable | `MemoryOS.exe`，15,506,332 bytes |
| Package smoke | PASS；clean path、0001→0003、12 MCP、两套 benchmark、sqlite-vec、Tree-sitter、HTTP/UI/CLI、restart，9.295 s |
| A33–A52 | 20/20 PASS；A47 按任务书允许路径记录真实外部 blocker，未声明模型效果 |

Playwright 的 7 个 skip 是明确的设备矩阵不适用项：移动布局断言只在 mobile 运行，会修改共享 fixture 或执行桌面审计的流程只在 desktop 运行。所有适用用例均通过。

## 证据与产物

- V2 clean baseline：`docs/verification/v2.1/v2-clean-baseline.json`
- CodingMemoryBench：`docs/verification/v2.1/coding-memory-bench.{json,html}`
- 100K full pipeline：`docs/verification/v2.1/full-pipeline-performance.json`
- real-agent/blocker：`docs/verification/v2.1/agent-ab.json`
- A33–A52：`docs/verification/v2.1/acceptance-summary.json`
- main smoke：`docs/verification/v2.1/main-release-smoke.json`
- 19-gate 总验证：`docs/verification/v2.1/verify-summary.json`
- package smoke：`docs/verification/package-smoke.json`
- V2.2 MarkupSafe 公共三组回放：`docs/verification/v2.2/markupsafe-public-smoke/{report,run-metadata}.json`
- AI calibration Requests 真实消融摘要：`benchmarks/ai_calibration_v1/evidence/requests-6028-real-agent-ablation-v1.json`
- Public BGE/RRF Shadow 摘要：`benchmarks/ai_calibration_v1/evidence/public-rrf-shadow-v1.json`
- wheel：`build/wheel/memoryos-2.1.0-py3-none-any.whl`
  - SHA-256 `C0280EC2A2AC5B79EA54C1CD6E2AE361E2255B3C97113C706A963867E1323033`
- Windows：`release/MemoryOS/MemoryOS.exe`
  - SHA-256 `B9330BDF6956F3A0BD9F41A80229DBD7FA561E9B430764A788C58C31B205F5FD`

## 已知边界

- 当前已有一个可放入隔离容器的真实 Codex runtime，但仍缺至少两个独立 provider/模型家族、短期凭据与 allowlisted gateway、完整 cost 计量、跨仓库 train/dev 证据，以及 50-task × 2 unseen-agent sealed promotion 矩阵；现有 Requests 结果只作探索性校准证据。
- Public RRF Shadow 只覆盖两个 public test 仓库，bootstrap 区间跨零且存在 Pandas 域回退；不能用其 19/81 比例替换生产 50/50 基线。下一轮需增加独立仓库，并直接做 frozen-baseline 与 candidate-weight 的成对 Agent Shadow，而不是重复同标签生成器的数据。
- Pytest 的 FastAPI/Starlette TestClient 与 Python 3.12 sqlite datetime adapter 发出弃用 warning；所有测试通过。
- PyInstaller 报告未安装可选 `tzdata`、`pysqlite2`、`MySQLdb`；MemoryOS 使用内置 SQLite，实际冻结包迁移与功能 smoke 已通过。
- 冻结 MCP 子进程有一条 `pydantic-settings` forward-reference warning；协议初始化、12 个工具和跨进程读写均通过。

产品边界仍是单机单用户、loopback-only、无云同步、无应用层静态加密、无全仓源码收藏。详见 `SECURITY.md`。
