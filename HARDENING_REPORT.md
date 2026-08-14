# MemoryOS V2.2 Hardening Report

日期：2026-08-14（Asia/Shanghai）

审查基线：`main @ 5dbf2916dc756ffb27ef0389cd0961316f2a3924`

实施起点：`e607d5f7c2f602aea7d5e44b8aed0e5d755feb5e`
实施分支：`codex/memoryos-hardening-v22`

## 1. Summary

本轮完成 H-001～H-008。三项 P0 均先在旧实现上复现失败，再修复；Source Anchor 基线改为不可变，CodingMemoryBench fixture 与真实产品路径评测分离，Claim/Relation Retrieval 在 SQL scope/status/time/entity 过滤后再限流。Current Truth 改为 SQL 双时态窗口和批量预取；性能证据按 FTS-first、Hybrid Local、Model-enhanced 三档陈述；`readiness.json` 成为当前校准状态的单一机器事实源。未接线的 staleness model 配置、provider 和 API 能力声明已删除，外部名称与实际算法对齐。

本报告不声明 production ready，也不把 fixture、synthetic performance 或小型 integration corpus 外推成真实 Coding Agent 效果。

## 2. Final acceptance gates

| Gate | Result | Evidence |
| --- | --- | --- |
| H-001 regression reproduced before fix | PASS | 旧实现运行 `tests/test_v2_freshness.py`：`3 failed, 5 passed in 3.08s` |
| Original anchor evidence immutable | PASS | `test_refresh_cannot_launder_changed_evidence_back_to_fresh`、`test_explicit_reanchor_creates_a_new_baseline_and_preserves_history` |
| Suspect cannot silently return fresh | PASS | `test_a20_a21_a22_git_fresh_moved_suspect_and_stale`、`test_refresh_cannot_launder_changed_evidence_back_to_fresh` |
| Production-path benchmark uses real services | PASS | `test_production_coding_bench_executes_real_services_with_isolated_gold`；实际调用 Database、MemoryService、RetrievalPipeline、ContextCompiler、Current Truth |
| Fixture score not presented as product correctness | PASS | fixture 报告含 `evidence_type=deterministic_fixture`、`effect_claim=none`、`production_path_executed=false`；UI/README 标为 Fixture Regression |
| >5000 claim retrieval regression | PASS | 6,001 个跨 scope decoy 后仍命中末尾 target；旧实现复现为 `1 failed in 0.68s` |
| Cross-scope claim retrieval safety | PASS | `test_claim_relation_channel_filters_scope_before_large_candidate_limit` |
| Bitemporal query count bounded | PASS | 1/10/1000 identities 均为 9 条 SQL；`test_current_truth_results_scale_without_query_count_scaling` |
| FTS-only benchmark correctly labeled | PASS | `100k-fts-first-core-pipeline.json`；实际 executed/contributing 仅 `fts` |
| Hybrid benchmark reports active channels | PASS | 10K/20K 报告中 FTS/vector/graph/temporal 均 executed/contributing，ANN 与 exact fallback 分开 |
| README/project status drift check | PASS | `python scripts/sync_project_status.py --check`；CI 已加入同一命令 |
| Ruff | PASS | `python -m ruff check memoryos tests scripts` |
| Mypy strict | PASS | `python -m mypy memoryos`：107 source files，0 issues |
| Pytest | PASS | `278 passed, 1 warning in 80.92s` |
| Frontend checks if touched | PASS | typecheck、lint、11 unit tests、production build；Playwright 9 passed / 7 intentionally skipped |
| Dependency audit | PASS | `pip-audit --local` 与 `pnpm audit --prod --audit-level high` 均为 no known vulnerabilities |

额外打包验证：Windows PyInstaller onedir 构建成功；V1→V2.2 production smoke 为 PASS，schema=`0004_anchor_observation_hardening`，12 个 MCP 工具、Tree-sitter、sqlite-vec、HTTP/UI/CLI、旧数据保留和重启持久化均通过，用时 10.170 秒。

## 3. P0 fixes completed

### H-001 Freshness Anchor Immutability

- `commit_sha/blob_sha/path/line/evidence_excerpt/excerpt_hash/context_hash` 保持原始基线语义；普通 refresh 不再改写。
- 新增 `observed_head/path/line_start/line_end/excerpt_hash/at`，只记录最近观测。
- refresh 始终与 original evidence 比较；fresh→moved→suspect/stale→无关提交不能洗回 fresh。
- 显式 re-anchor 通过创建新 `SourceAnchorRow` 完成；旧 anchor 和 audit history 保留，ClaimEvidence 显式转向新 anchor。
- 0003→0004、downgrade→replay、backup/restore 均有回归覆盖。

### H-002 Production-path Benchmark

- 保留原 CodingMemoryBench 作为 deterministic fixture regression，不再作为产品正确率。
- 新增 production-path integration suite，runtime 与 gold 分离，并实际经过 Database、MemoryService、RetrievalPipeline、TaskAwareContextCompiler、TruthMaintenanceService。
- Retrieval 覆盖 target、sibling scope、stale、candidate、archived 和 negative constraint；Temporal 覆盖 valid/known time、supersede history、stale、archive/restore history；Conflict 覆盖 resolved/contested。
- 破坏 `RetrievalPipeline.search` 的测试会让 integration benchmark 直接失败，证明它没有复制近似检索逻辑。
- 最新 production suite：retrieval 4、context 1、temporal 7、conflict 2，所有 integration gates 为 true；外部 embedding/model provider 未启用，实际 contributing channel 仅 FTS。

### H-003 Claim/Relation Retrieval Correctness

- 删除过滤前 `.limit(5000)` 路径；scope、memory/claim status、valid/known time、TTL、archive、entity 条件先进入 SQL，再执行 deterministic ranking 和 candidate limit。
- 新增 scope/name 与 claim subject/status/recorded 复合索引及 0004 migration。
- 一跳 relation expansion 对 related claim 再执行相同 eligibility gate；不扩展 multi-hop。
- 内部兼容 trace key `graph` 保留，产品名称改为 Claim/Relation Retrieval。

## 4. P1 engineering completed

### H-004 Bitemporal Query Scaling

- `visible_versions()` 将 transaction/valid 半开区间过滤下推 SQL，并以 SQLite `row_number()` 按 claim 选择最新可见版本。
- `current_truth()` 批量加载 ClaimIdentity、Entity、archive/restore audit、evidence、version history 和 relations，移除按版本 `session.get()` 的 N+1 路径。
- archive/restore 同时钟刻度问题在集成评测中被复现；同一 memory 的两类 audit transaction time 现在严格单调，冻结时钟测试通过。
- API schema 保持不变；valid/known boundary 与 archive/restore history 均有回归。

### H-005 Honest Performance Benchmarks

- Tier 1 正式命名为 `100K FTS-first Core Pipeline`，报告真实 requested/executed/contributing/degraded channels。
- Tier 2 对 10K 和 20K 两档均使用本地真实 `BAAI/bge-small-en-v1.5` 向量，运行 sqlite-vec ANN 与强制 exact fallback，并记录 provider revision/model hash、数据量、通道、backend、fallback、platform 与 P50/P95。
- Tier 3 runner 独立于 deterministic CI，要求显式模型 endpoint；记录 provider/model/timeout、usage/cost 可用性、fallback 和实际通道。当前没有兼容 reranker/relationship endpoint，因此状态是 `not_executed`，没有伪造延迟或成本。
- `performance-tiers.json` 绑定各原始报告 SHA-256；测试会检测报告漂移或通道夸大。

### H-006 Project Status Single Source of Truth

- `benchmarks/ai_calibration_v1/readiness.json` 是 V2.2 当前校准状态唯一机器事实源。
- `scripts/sync_project_status.py --write/--check` 仅更新 README/PROJECT_STATUS 的 marker 区块。
- GitHub Actions 在 lint 后检查 status drift；历史 V2.1 verification artifacts 不由同步脚本改写。

## 5. P2 cleanup completed

- H-007：全仓引用审计确认 `staleness_model` 和 `OpenAICompatibleStalenessJudge` 未接入主链路；配置字段、Protocol、provider 实现及公开 API capability 已删除。Freshness 明确为 deterministic Git/source-anchor path。
- H-008：产品/UI 名称使用 Deterministic Query Planner、Claim/Relation Retrieval、Lexical MMR、100K FTS-first Core Pipeline、Fixture Regression Result；兼容敏感的内部 `graph`/`mmr` key 保持不变，历史 CHANGELOG/verification snapshot 未重写。

## 6. Database migration

新增 `0004_anchor_observation_hardening`，down revision 为 `0003_reality_intelligence_hardening`。迁移使用显式 Alembic operations：

- 为 `source_anchors` 增加 6 个 nullable observation 字段，并从旧 baseline/cache 保守回填；不生成新事实、不改原 evidence hash。
- 新增 `ix_entities_scope_name` 与 `ix_claims_subject_status_recorded`。
- downgrade 删除新增索引与字段；replay 不增加 anchor/claim history 行。
- backup schema current 提升至 0004，同时继续接受 0001/0002/0003 导入。

## 7. Tests run and exact results

```text
python -m ruff check memoryos tests scripts
PASS

python -m ruff format --check memoryos tests scripts
PASS (212 files already formatted)

python -m mypy memoryos
PASS: Success: no issues found in 107 source files

python -m pytest -q
PASS: 278 passed, 1 warning in 80.92s

pnpm typecheck
PASS
pnpm lint
PASS
pnpm test
PASS: 4 files / 11 tests
pnpm build
PASS: 1745 modules transformed

pnpm test:e2e
PASS: 9 passed, 7 intentionally skipped

python -m pip_audit --local --progress-spinner off
PASS: No known vulnerabilities found

pnpm audit --prod --audit-level high
PASS: No known vulnerabilities found
```

Pytest 的唯一 warning 是上游 `fastapi.testclient` 对 Starlette/httpx 兼容层的 deprecation warning；package smoke 另出现一个 pydantic-settings incomplete forward-reference warning，均未导致 gate 失败。

## 8. Benchmark before/after

Current Truth 使用同一台 Windows 11 / Python 3.12.13 机器、同一 1/10/1000 identity corpus、每档 7 个 measured rounds。它是 deterministic synthetic performance evidence，不是 Agent 效果证据。

| Identities | Baseline SQL | Hardened SQL | Baseline P50/P95 ms | Hardened P50/P95 ms |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 4,049 | 9 | 557.323 / 587.757 | 1.997 / 2.584 |
| 10 | 4,076 | 9 | 557.433 / 607.847 | 2.684 / 2.916 |
| 1,000 | 7,046 | 9 | 1217.033 / 1311.148 | 76.094 / 82.460 |

性能分层实测：

| Tier | Corpus / actual capability | Search P50/P95 ms | Context P50/P95 ms |
| --- | --- | ---: | ---: |
| 1 | 100K memories；FTS-only contributing | 17.715 / 20.598 | 239.949 / 263.738 |
| 2 ANN | 10K real BGE embeddings + 10K claims + 100 relations；FTS/vector/Claim-Relation/temporal | 278.454 / 313.424 | 383.943 / 410.587 |
| 2 exact | 同一 10K corpus，forced NumPy fallback | 1679.675 / 1733.581 | 1752.499 / 1783.612 |
| 2 ANN | 20K real BGE embeddings + 20K claims + 200 relations；FTS/vector/Claim-Relation/temporal | 322.170 / 375.527 | 442.152 / 451.078 |
| 2 exact | 同一 20K corpus，forced NumPy fallback | 3054.057 / 3165.333 | 3142.234 / 3253.577 |
| 3 | Model-enhanced | 未执行 | 未执行 |

Tier 2 的 `ClaimVersion` history count 为 0，temporal contribution 来自 Claim valid intervals；真正的 bitemporal version-history 扩展性由上方 Current Truth benchmark 单独验证。

## 9. Security invariants verified

- Loopback bind：`test_non_loopback_bind_address_is_rejected`。
- Local token + Origin allowlist：`test_write_auth_origin_and_api_lifecycle`、`test_cross_port_origin_cannot_reuse_ui_cookie`。
- Agent/candidate-first：`test_candidate_lifecycle_and_illegal_transition`；本轮没有自动激活路径。
- Stale hard exclusion：`test_stale_is_a_hard_gate_and_never_a_learned_weight` 及 production retrieval hard negatives。
- Cross-scope isolation：6,001-decoy Claim/Relation regression、branch/context scope tests。
- Provider failure does not mutate truth：`test_a39_provider_failure_abstains_without_mutating_truth`、provider timeout/invalid JSON tests。
- Secret/prompt safety：structured logging redaction 与 source redaction tests；benchmark reports 不记录 API key 或完整 prompt。
- Archive truth protection：`test_a51_archive_is_reversible_and_protects_sole_truth`；archive/restore historical reconstruction 和 monotonic timestamp regression。
- Shadow isolation：candidate/RRF/routing profiles 仍需显式 shadow；没有自动 production activation。
- Future solution leakage：real-workload cutoff/source publication、manifest、cross-project canary 与 protocol tests 继续通过。

## 10. Remaining limitations and exploratory claims

- Tier 2 是 synthetic corpus + real local embedding 的性能证据，不测真实 coding task success；没有 cross-encoder reranker、relationship model 或 ClaimVersion history。
- Tier 3 因没有兼容模型 endpoint 未执行。Runner 会把 token/cost 标为 provider contract 不可用，而不是猜测；真实 endpoint 接入后仍需单独执行。
- Production-path CodingMemoryBench 是小型 deterministic integration corpus；它证明主链路接线和场景正确性，不估计泛化准确率或 Agent 效果，且本次实际 contributing retrieval channel 仅 FTS。
- AI calibration readiness 仍是 `protocol_ready_evidence_pending`：9 个有效 real-agent pairs、1 个有效模型家族/provider、0 个 sealed promotion tasks；production weights/profile 继续冻结。
- 本分支完成 Windows package smoke，但 clean-main release smoke 必须在合并后的干净 `main` 上执行，本轮分支不能伪造该门禁。

仍属 exploratory：public BGE retrieval prior、model-only blind review、9-pair real-agent ablation、query-adaptive routing shadow、所有 learned weight candidate。它们均不构成 production promotion。

## 11. Files changed

- Core correctness：`memoryos/freshness/{anchors.py,git_compare.py}`、`memoryos/retrieval_v2/stages.py`、`memoryos/claims/{versioning.py,truth.py}`、`memoryos/health/service.py`、`memoryos/db/models.py`、`memoryos/backup/service.py`。
- Migration/provider/API：`memoryos/db/migrations/versions/0004_anchor_observation_hardening.py`、`memoryos/config.py`、`memoryos/providers/{base.py,openai_compatible.py}`、`memoryos/api/app.py`。
- Evaluation/benchmarks：`memoryos/evaluation/{coding_memory_bench.py,coding_memory_bench_production.py,__init__.py,report.py,public_shadow.py,retrieval_weight_calibration.py}`、`scripts/{coding_memory_bench.py,benchmark_v21_pipeline.py,benchmark_current_truth.py,benchmark_hybrid_pipeline.py,benchmark_model_enhanced_pipeline.py}`。
- Release/status tooling：`.github/workflows/ci.yml`、`scripts/{sync_project_status.py,verify_v21.py,acceptance_v2.py,acceptance_v21.py,production_smoke.py,main_release_smoke.py}`、`pyproject.toml`。
- Documentation/evidence：`README.md`、`PROJECT_STATUS.md`、`ARCHITECTURE.md`、`docs/{ACCEPTANCE.md,REALITY_INTELLIGENCE_V2_1.md,PERFORMANCE_TIERS_V2_2.md}`、`benchmarks/ai_calibration_v1/README.md`、`docs/verification/v2.2/*`。
- Tests/UI：`tests/test_{backup_restore,database_and_core,v21_hardening,v2_freshness,project_status_sync,v22_benchmark_hardening,v22_capability_hardening,v22_performance_evidence,v22_retrieval_hardening,v22_truth_query_hardening}.py`、`web/src/pages/{RetrievalDebuggerPage.tsx,BenchmarkDashboardPage.tsx}`、`web/tests/intelligence-pages.test.tsx`、`web/e2e/memoryos.spec.ts`。

## 12. Recommended next step

合并后只做一项：在干净 `main` 上运行完整 clean-main release verification，生成与合并 commit 绑定的最终 package/release evidence；在该门禁通过前不要发布 V2.2 二进制或激活任何 learned profile。

---

## V2.3 Minimum Sufficient Context 补充记录（2026-08-15）

上文是 V2.2 硬化阶段的历史报告，其测试计数、二进制哈希和结论不回写。V2.3 在该基线之上新增可回退的最小充分上下文编译层：

- `legacy / msc_shadow / msc` 三模式，真实证据门禁通过前默认保持 legacy；
- 旧 `budget` 字符语义不变，新增带 exact/estimated 来源的 Token 预算与完整 payload 分账；
- 确定性 Context Atom、Pinned Constraint/Contested Bundle 原子安全、exact dedup 和一次 Explain 证据回溯；
- 显式 `previous_context_id` Delta，以及 Scope/TTL/Policy/Tokenizer/完整性失效时的安全 Full Rebase；
- `0005_context_efficiency` 迁移和不进长期备份的可丢弃 Snapshot 缓存；
- 启动时固定的 all/core/governance/debug MCP Profile 及四个规范 Schema Snapshot；
- 不修改旧三臂枚举的独立 Context Efficiency Study，包含 Provider input/output/cached、成本、延迟、完整记忆/Schema 分账、每成功任务成本、受限 ROI、0.5/0.65/0.8/0.9 Delta 阈值矩阵，以及非劣、安全、透明度、power 和最差组门禁。

确定性证据位于 `docs/verification/v2.3/`。Context Efficiency dry run 不包含真实 Provider Usage，
只是 protocol fixture，机器结论为 `effect_claim=none` 和 `default_mode_decision=legacy`。
初始 2 个百分点的预注册非劣界限在 10% 不一致率假设下约需 1,546 个独立配对任务；
在样本、Provider Usage、安全和最差组门禁完成前，不宣称 MSC 已降低真实 Agent Token 或改善成功率。

V2.3 本地分支验证结果：V2.3 专项 36 passed；Ruff/format PASS，Mypy 115 个源码文件 0 issues，pytest
332 passed/0 failed（两条已知上游 warning）；前端 typecheck/ESLint PASS，Vitest 11/11 PASS，
Vite 生产构建 1,745 modules PASS。这些数字是当前源码树的回归证据，不是与 merge commit
绑定的发行证据，也不是 MSC 效果证据。

完整语义、配置、证据和限制见 `docs/MINIMUM_SUFFICIENT_CONTEXT.md`。V2.3 Windows onedir
仍必须在合并后干净 `main` 上重建并运行 V1→`0005_context_efficiency` smoke；旧 V2.2
包保留为历史证据，不重标为 V2.3。
