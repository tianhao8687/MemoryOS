# MemoryOS 项目状态

## 当前结论

- 版本：V2.1.0
- 状态：**V2.1 已合并 `main`，A33–A52 全部通过，发布验收完成**
- 日期：2026-08-10（Asia/Shanghai）
- 干净 V2 基线：commit `b0cae26dfab0141876ceffa1fde97cc5e2b92591`，dirty=false，16/16 PASS
- V2.1 合并与 clean-main 发布基线：commit `eaf10ba700455513f4eb4a392f4c042a6b4ea125`，dirty=false，19/19 release gates PASS
- 最终验收入口：`.\.venv\Scripts\python.exe scripts\verify_v21.py`
- 验收范围：V1 A01–A14、V2 A15–A32 回归，V2.1 A33–A52

MemoryOS V2.1 已完成当前环境内可实现和可测量的 Reality Intelligence hardening。真实 coding-agent endpoint 与凭据未提供，因此 A47 使用任务书允许的明确 `external_blocker` 路径：完成样本为 0，`effect_claim=none`。50-task fixture 仅证明 harness/metrics/CI plumbing，不是模型效果证据。

## V2.1 交付范围

| 领域 | 已交付内容 | 状态 |
| --- | --- | --- |
| Migration | 0001/0002 显式 immutable operations；0003 回填版本，可 downgrade/replay | Complete |
| Bitemporal Truth | ClaimIdentity + append-only ClaimVersion，valid/transaction 双时间与 reason/actor | Complete |
| Conflict 2.0 | deterministic uncertain router、bounded model、Possible Conflict 审计/人工处理、abstain safety | Complete |
| ANN | sqlite-vec 持久化 namespace、实时 upsert/search、doctor/status/rebuild、exact fallback | Complete |
| CodingMemoryBench | input/gold 隔离、hard negatives、baseline/V2/V2+model、满分警告 | Complete |
| Agent A/B | ≥50 paired harness 与全指标 fixture；真实 endpoint 缺失 blocker | External blocker |
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
| Backend wheel | `memoryos-2.1.0-py3-none-any.whl`，135,473 bytes |
| Windows executable | `MemoryOS.exe`，15,506,332 bytes |
| Package smoke | PASS；clean path、0001→0003、12 MCP、两套 benchmark、sqlite-vec、Tree-sitter、HTTP/UI/CLI、restart，9.264 s |
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
- wheel：`build/wheel/memoryos-2.1.0-py3-none-any.whl`
  - SHA-256 `C0280EC2A2AC5B79EA54C1CD6E2AE361E2255B3C97113C706A963867E1323033`
- Windows：`release/MemoryOS/MemoryOS.exe`
  - SHA-256 `A4E17EC4ADAD3B57D760346C1BC1D25297A458349C4A7964DE7FF4743C935C6C`

## 已知边界

- 唯一外部阻塞项是缺少真实 coding-agent endpoint/model/credentials；不做效果声明。
- Pytest 的 FastAPI/Starlette TestClient 与 Python 3.12 sqlite datetime adapter 发出弃用 warning；所有测试通过。
- PyInstaller 报告未安装可选 `tzdata`、`pysqlite2`、`MySQLdb`；MemoryOS 使用内置 SQLite，实际冻结包迁移与功能 smoke 已通过。
- 冻结 MCP 子进程有一条 `pydantic-settings` forward-reference warning；协议初始化、12 个工具和跨进程读写均通过。

产品边界仍是单机单用户、loopback-only、无云同步、无应用层静态加密、无全仓源码收藏。详见 `SECURITY.md`。
