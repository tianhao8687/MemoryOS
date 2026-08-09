# MemoryOS V1.0 验收证据

本文档将总任务书的 A01–A14 强制验收项映射到可重复执行的测试和产物。最终唯一入口是：

```powershell
.\.venv\Scripts\python.exe scripts\verify.py
```

脚本在首个失败门禁处返回非零，并将已执行步骤写入 `docs/verification/verify-summary.json`。只有所有 14 个质量/产物门禁通过才写入 `result: PASS`。

## A01–A14

| ID | 要求 | 自动证据 | 关键断言 |
| --- | --- | --- | --- |
| A01 Persistence | active 写入后重启仍可读 | `scripts/production_smoke.py` | MCP propose/confirm，停止发行包服务，重启后 HTTP 搜索到同一 ID |
| A02 Cross-client | MCP 写、HTTP/CLI 读同一数据 | `tests/test_mcp_stdio.py::test_real_stdio_cross_client_persistence` + package smoke | 真实 stdio 子进程写入后，TestClient 通过 HTTP 读取；打包 CLI 对同一 data dir 执行 status |
| A03 Conflict | 同 repo/key 冲突不静默覆盖 | `tests/test_database_and_core.py::test_conflict_requires_resolution_and_preserves_supersession_chain` | 无 strategy 确认时抛出 `ConflictDetectedError`，旧 active 值保留 |
| A04 Supersede | 新值确认后旧值 superseded，history 完整 | 同 A03 测试 | 新值 `supersedes_id` 指向旧值，旧值状态为 `superseded`，explain 含关系 |
| A05 TTL | 过期 working memory 不进入默认 context | `tests/test_database_and_core.py::test_ttl_expiration_is_excluded_but_auditable` | 默认搜索不返回，状态转 `expired`，audit 含 `expire` |
| A06 Branch Isolation | feature 分支记忆不污染 main | `tests/test_database_and_core.py::test_branch_scope_isolation_in_context` | 相同 repo 下 feature context 可见，main context 不可见 |
| A07 Provenance | active memory 至少一个 source 且可 explain | `tests/test_database_and_core.py::test_provenance_fts_and_secret_redaction` + MCP test | explain 含 source、64 字符 SHA-256、audit 和关系 |
| A08 Offline | 无模型/无网络仍可运行检索 | `tests/test_providers.py::test_heuristic_extractor_works_offline_and_only_returns_candidates` + `scripts/benchmark_search.py` | heuristic 本地提取，10,000 条基准明确使用 `mode: fts5` |
| A09 Provider Failure | 超时/非法 JSON 不污染 DB | `tests/test_providers.py` 中 invalid JSON 和 timeout 测试 | 均返回 `ProviderError`，`memories` 行数仍为 0 |
| A10 Security | 无 token 或恶意 Origin 的写请求被拒 | `tests/test_api_security.py` | 无 token 返回 401，恶意 Origin 返回 403，非 loopback bind 配置被拒绝 |
| A11 Backup | 备份/恢复后核心数据一致 | `tests/test_backup_restore.py` | SQLite round trip、版本化 JSONL 导入导出、safety backup、损坏 DB 拒绝且活数据不变 |
| A12 No Source-code Hoarding | 仓库扫描不批量存源码 | `tests/test_git_integration.py::test_repository_identity_survives_path_move_and_does_not_hoard_source` | 识别移动后的同一 remote 仓库，`memories` 行数仍为 0 |
| A13 UI E2E | 候选确认、冲突、搜索、忘却、explain | `web/e2e/memoryos.spec.ts` | 真实 FastAPI + Chromium 完成所有主流程，desktop/mobile 视口，axe 0 violation，console/page error 为 0 |
| A14 Package Smoke | 干净路径的 PyInstaller 产物可运行 | `scripts/build_windows.py` + `scripts/production_smoke.py` | 将整个发行目录复制到带空格的临时路径，验证 HTTP/UI/7 MCP tools/CLI/重启持久化 |

## 质量和构建门禁

`scripts/verify.py` 执行下列 14 个命令级门禁：

1. backend import
2. Ruff lint
3. Ruff format check
4. Mypy strict
5. Pytest
6. TypeScript typecheck
7. ESLint（zero warnings）
8. Vitest
9. Vite production build
10. Playwright E2E
11. 10,000-record FTS-only benchmark
12. backend wheel
13. Windows PyInstaller
14. packaged production smoke

## 性能证据

`docs/verification/performance.json` 保存当前机器的真实数据，不使用造出的目标数字。基准每次创建临时数据库，批量写入 10,000 条带 provenance 的 active memory，预热后分别执行 7 次 search 和 context。任一次超过 1,000 ms 会导致脚本失败。

## UI 视觉证据

生成的设计参考位于 `docs/design/`，真实应用截图位于 `docs/verification/`。验证视口包括 1536×1024 desktop 和 412×915 mobile；主要对比点是 graphite 导航、cool-gray 画布、teal active 状态、amber conflict 状态、高密度 ledger/table 和右侧 inspector。

Playwright 自动验收之外，还使用应用内浏览器检查 desktop/mobile 布局、console、抽屉尺寸和水平溢出。移动端最终测量为 body/document/drawer 宽度均不超过 412 px，drawer 从 54 px 顶部导航下方开始。

## 产物

- Python wheel：`build/wheel/memoryos-1.0.0-py3-none-any.whl`
- Windows 发行目录：`release/MemoryOS/`
- 主可执行文件：`release/MemoryOS/MemoryOS.exe`
- 性能报告：`docs/verification/performance.json`
- 发行包冒烟报告：`docs/verification/package-smoke.json`
- 全门禁汇总：`docs/verification/verify-summary.json`
