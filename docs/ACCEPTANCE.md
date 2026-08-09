# MemoryOS V2.0 验收证据

唯一全量入口：

```powershell
.\.venv\Scripts\python.exe scripts\verify.py
```

脚本 fail-fast，并把 V2 汇总写入 `docs/verification/v2/verify-summary.json`。A15–A32 机器清单写入 `docs/verification/v2/acceptance-summary.json`；MemoryBench 原始报告为 JSON 和 HTML。

## A01–A14 V1 回归

V1 frozen baseline 位于 `docs/verification/v2/v1-baseline.json`（commit `83df3903954751188c731ac097b93e2a2c71d26c`，14/14 PASS）。V2 的全量 Pytest、Playwright、10k regression 和 package smoke 会再次运行原场景。

| ID | 要求 | 自动证据 |
| --- | --- | --- |
| A01 Persistence | active 重启可读 | packaged smoke 两次启动 |
| A02 Cross-client | MCP 写，HTTP/CLI 读 | `tests/test_mcp_stdio.py` + packaged smoke |
| A03 Conflict | 冲突不静默覆盖 | `tests/test_database_and_core.py` |
| A04 Supersede | replacement/history 完整 | `tests/test_database_and_core.py` |
| A05 TTL | 过期默认不可检索且可审计 | `tests/test_database_and_core.py` |
| A06 Branch Isolation | sibling branch 不泄漏 | V1 core test + V2 scope-narrowing test |
| A07 Provenance | source/hash/explain | core + MCP tests |
| A08 Offline | 无模型仍可用 | provider tests + FTS benchmarks |
| A09 Provider Failure | timeout/非法 JSON 不污染 DB | `tests/test_providers.py` |
| A10 Security | token/origin/loopback | `tests/test_api_security.py` |
| A11 Backup | 备份、恢复、损坏拒绝 | `tests/test_backup_restore.py` |
| A12 No Hoarding | 不批量收藏源码 | `tests/test_git_integration.py` |
| A13 UI E2E | 核心工作流、desktop/mobile、axe | `web/e2e/memoryos.spec.ts` |
| A14 Package Smoke | clean path EXE/UI/MCP/CLI/restart | `scripts/production_smoke.py` |

## A15–A32 V2 强制验收

| ID | 要求 | 自动证据与关键断言 |
| --- | --- | --- |
| A15 Claim Normalization | `tests/test_v2_claims_truth.py`：一句拆多个 Claim；每个 exact span 可回切原文；同义/大小写改写保持 equivalent |
| A16 Entity Resolution | 同 repo/type 的 Postgres/PostgreSQL alias 合并；跨 repo/type ID 不同 |
| A17 Semantic Conflict | 不同 key、相同语义维度的 FastAPI/Django 进入 review |
| A18 Truth State | resolved/contested/stale/unknown 均由 Current Truth 状态机返回 |
| A19 Bitemporal | valid-time 与 known-at 分离测试，历史、尚未知、当前结果不同 |
| A20 Git Fresh | `tests/test_v2_freshness.py`：真实 Git repo 未变 anchor 为 fresh；四语言 Tree-sitter symbol test |
| A21 Git Moved | `git mv` 后 blob/symbol 重定位为 moved/fresh-like |
| A22 Git Stale | 实质修改为 suspect/stale、删除为 stale；replacement 仍是 candidate |
| A23 Retrieval Trace | persisted run/manifest 含 FTS/vector/graph/temporal rank、fusion/filter/reason |
| A24 RRF/Rerank Fallback | failing embedding 返回 FTS fallback；EmbeddingRow 仍为 0；exact/optional ANN fallback 单测 |
| A25 Context Contest | contested group 双方均入 context；sibling branch 完全不进 candidate manifest |
| A26 Consolidation | 三个独立 source、跨七日生成 candidate、lineage 为 consolidated_from、不激活 |
| A27 Counterevidence | 反证输出 contested/counterevidence，不产单一确定事实 |
| A28 Feedback | feedback 必须属于 RetrievalRun，可审计，仅改 utility factor，fact status unchanged |
| A29 MemoryBench | `memorybench-report.json`：E100/R250/C200/T120/G120/L80/X150/A30/P100k，均含 V1 baseline |
| A30 Real Model Truthfulness | fixture 明确 `real_model:false` 和 `harness_validation_only`；真实模型为 `external_blocker/not_evaluated` |
| A31 V1 Regression | frozen V1 14/14 + V2 全量原测试 + 10k V1 benchmark + package regression |
| A32 Package Upgrade | production smoke 创建真实 0001 DB，packaged EXE 自动迁移 0002，旧/新 memory 经 12-tool MCP、HTTP、UI、CLI、restart 共用；冻结包实际加载 Tree-sitter grammar 创建 symbol anchor |

## MemoryBench 门槛

冻结配置、seed、commit、dirty state、provider/model、evidence type 和 config hash 全部进入报告。test 不参与调参。

| 门槛 | 目标 | 报告字段 |
| --- | --- | --- |
| Branch leakage | 0 | `suites.context.v2.branch_leakage` |
| Temporal accuracy | ≥0.95 | `suites.temporal.v2.accuracy` |
| Conflict macro F1 | ≥0.85 | `suites.conflict.v2.f1` |
| Git stale recall | ≥0.90 | `suites.git_freshness.v2.stale_recall` |
| Context selected precision | ≥0.80 | `suites.context.v2.selected_precision` |
| Retrieval Recall@5 | V1 +10% 或 ≥0.90 | `suites.retrieval` |
| Redundancy | ≤0.20 | `suites.context.v2.redundancy_rate` |
| 100k FTS5 P95，无 reranker | <500 ms | `suites.performance_100k.v2.p95_ms` |

真实 coding-agent A/B 需要外部模型/harness。本环境未配置，因此该效果项保持外部阻塞；这是 A30 要求的诚实结论，不把 fixture 成功率当产品准确率。

## 命令级门禁

`scripts/verify.py` 执行 16 个步骤：

1. backend import
2. Ruff lint
3. Ruff format check
4. Mypy strict
5. Pytest
6. MemoryBench V2
7. TypeScript typecheck
8. ESLint zero warnings
9. Vitest
10. Vite production build
11. Playwright desktop/mobile + axe
12. V1 10,000-record FTS/context regression
13. backend wheel
14. Windows PyInstaller onedir
15. packaged V1→V2 production smoke
16. A15–A32 evidence manifest

## 产物

- wheel：`build/wheel/memoryos-2.0.0-py3-none-any.whl`
- Windows：`release/MemoryOS/MemoryOS.exe`（需保留整个 onedir）
- MemoryBench：`docs/verification/v2/memorybench-report.{json,html}`
- A15–A32：`docs/verification/v2/acceptance-summary.json`
- package smoke：`docs/verification/package-smoke.json`
- V2 verify：`docs/verification/v2/verify-summary.json`
