# MemoryOS 项目状态

## 当前结论

- 版本：V2.0.0
- 状态：**V2 实现与本地可测验收完成；真实模型效果声明受外部条件阻塞**
- 最后全量验收：2026-08-10（Asia/Shanghai）
- 唯一验收入口：`.\.venv\Scripts\python.exe scripts\verify.py`
- 最后结果：`PASS`，16/16 命令级门禁 exit code 0
- 验收场景：V1 A01–A14 回归与 V2 A15–A32 均有机器可读证据

MemoryOS V2 已按深化技术总任务书完成可在当前环境实现和测量的内容。所有本地质量、性能、升级和打包门禁通过。真实 coding-agent 的 30 任务 A/B 尚无可用模型端点，因此只运行了明确标注为 harness-only 的确定性夹具；项目不据此声明真实模型收益。

## V2 交付范围

| 领域 | 已交付内容 | 状态 |
| --- | --- | --- |
| V1 兼容基线 | 原有生命周期、provenance、备份恢复、7 个 MCP 工具和 A01–A14 全量保留 | Complete |
| Claim Intelligence | claim 原子化、证据 span、entity/alias、关系图、跨 key 语义冲突 | Complete |
| Current Truth | resolved/contested/stale/unknown、valid/system 双时间、历史查询 | Complete |
| Git Freshness | commit/blob/symbol anchor，Python/TypeScript/JavaScript/Rust Tree-sitter，moved/stale/suspect | Complete |
| Retrieval V2 | query planner、FTS/vector/graph/temporal RRF、rerank fallback、MMR、完整 trace | Complete |
| Task Context | 严格 scope chain、coverage、utility/budget manifest、contested 双方强制呈现 | Complete |
| Consolidation / Feedback | 跨来源候选、counterevidence、lineage、人工确认、retrieval-run 反馈审计 | Complete |
| Provider / Vector | OpenAI-compatible interfaces、exact NumPy 索引、可选 sqlite-vec ANN、离线降级 | Complete |
| 接口与 UI | 12 个 stdio MCP 工具、CLI、HTTP API、原 8 页加 6 个 Intelligence 工作台页面 | Complete |
| MemoryBench V2 | 9 套评测、固定 seed/config、基线对照、JSON/HTML 报告、真实/夹具标签 | Complete |
| 发布与升级 | wheel、Windows onedir、V1 `0001` 数据库原地迁移至 `0002`、联合生产冒烟 | Complete |

## 最后验收快照

| 门禁 | 实测结果 |
| --- | --- |
| Backend import | `memoryos.__version__ == 2.0.0` |
| Ruff lint / format | PASS / PASS，93 files formatted |
| Mypy | 67 source files，0 issues；额外全仓 strict 检查 89 source files，0 issues |
| Pytest | 54 passed |
| TypeScript / ESLint | 0 errors / 0 warnings |
| Vitest | 3 files，9 passed |
| Vite production build | 1,743 modules；JS 348.50 kB（gzip 104.59 kB） |
| Playwright | 8 applicable passed，6 intentional device-matrix skips；desktop + mobile |
| Accessibility | desktop overview 与 Intelligence workbench axe violations 0 |
| 10,000-record FTS/context | search median 60.074 ms / max 84.320 ms；context median 103.453 ms / max 133.132 ms |
| MemoryBench measured gates | 8/8 passed；100k V2 search P95 0.0339 ms |
| Backend wheel | `memoryos-2.0.0-py3-none-any.whl`，107,988 bytes |
| Windows package | `MemoryOS.exe`，15,454,647 bytes；完整 onedir 构建成功 |
| Package smoke | PASS；clean path、V1→V2、12 MCP tools、bundled Tree-sitter、HTTP/UI/CLI、restart persistence，9.469 s |
| A15–A32 manifest | 18/18 PASS；外部阻塞单独记录，不伪装为真实模型结果 |

Playwright 的 6 个 skip 是测试矩阵中的明确不适用项：移动布局断言只在 mobile 项目运行，会修改共享 fixture 的流程只在 desktop 项目运行。所有适用用例均通过。

## MemoryBench 结论

- E Extraction：100 个手工 gold case，V2 macro F1 1.0；V1 snapshot 约 0.950。
- R Retrieval：250 个 query，V2 Recall@5 / MRR / nDCG 均为 1.0；完整候选 trace 落盘。
- C/T/G/L/X：冲突、时间、Git freshness、consolidation、context 的本地 gate 全部通过。
- Context：selected precision 1.0、coverage 1.0、redundancy 0、branch leakage 0。
- P 100k Search：80 次查询/variant，V2 P95 0.0339 ms，低于 500 ms 门槛。
- Agent A/B：30 任务夹具仅验证 runner、指标和报告链路；真实模型状态为 `external_blocker`，`effect_claim=not_evaluated`。

## 可交付产物

- 源码：`memoryos/`、`web/`
- 数据库迁移：`memoryos/db/migrations/versions/0001_initial.py`、`0002_memory_intelligence.py`
- 一键验收：`scripts/verify.py`
- V2 验收证据：`docs/verification/v2/acceptance-summary.json`
- MemoryBench JSON/HTML：`docs/verification/v2/memorybench-report.json`、`memorybench-report.html`
- 全门禁报告：`docs/verification/v2/verify-summary.json`
- 性能报告：`docs/verification/performance.json`
- 发行包冒烟报告：`docs/verification/package-smoke.json`
- Python wheel：`build/wheel/memoryos-2.0.0-py3-none-any.whl`
  - SHA-256 `1265C5D05133CDE36D50ABE18392F8F3270E1ACD71CFA28AC70EBE698402EB72`
- Windows 发行目录：`release/MemoryOS/`
- 可执行文件：`release/MemoryOS/MemoryOS.exe`
  - SHA-256 `BD520EBE322CAD222282E29197EC45B224D0E33DB82F2F8C62FC3BD967875300`

## 已知非阻断项

- 缺少真实 coding-agent harness/model endpoint，真实模型 A/B 效果未评估；这是唯一外部阻塞项。
- Pytest 显示一条 FastAPI/Starlette TestClient 适配层的上游弃用警告；54 项测试均通过。
- PyInstaller 会报告未安装可选 `tzdata`、`pysqlite2`、`MySQLdb`；MemoryOS 使用 Python 内置 SQLite，冻结包迁移、FTS5、MCP、HTTP 与重启均已通过。
- 冻结 MCP 子进程启动时出现一条 `pydantic-settings` forward-reference 警告；协议初始化、12 个工具和读写调用均通过。

产品安全边界记录在 `SECURITY.md`：单机单用户、无应用层静态加密、读 API 依赖 loopback/OS 边界、无云同步，也不会自动操作 Cursor/Claude Code 图形界面。
