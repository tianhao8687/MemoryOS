# MemoryOS

MemoryOS V2.0 是面向编码 Agent 的本地优先 Memory Intelligence 层。它在 V1 可审计记忆库之上增加 Claim Graph、实体解析、双时态 Current Truth、Git-aware freshness、可解释 Retrieval 2.0、任务感知 Context Compiler、巩固与反馈闭环，并让 MCP、HTTP、CLI 和 React Workbench 共享同一 SQLite 事实源。

当前版本：`2.0.0`。唯一全量验收入口为：

```powershell
.\.venv\Scripts\python.exe scripts\verify.py
```

验收映射见 [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)，机器报告位于 `docs/verification/v2/`。

## V2 能力

- 保留 V1 的五级 scope、六类 memory、candidate-first 生命周期、来源、审计、TTL、逻辑忘却、备份和 7 个 MCP 工具。
- 从 evidence span 生成标准化 Claim；实体别名只在同 scope/type 内解析，可审计合并与 redirect。
- Claim 关系支持 equivalent/supports/contradicts/supersedes 等；Current Truth 返回 `resolved | contested | stale | unknown`。
- `valid_from/valid_to` 与 `recorded_at` 分离，支持 valid-time 和 known-at 双时态查询。
- Source Anchor 使用 Tree-sitter 解析 Python、TypeScript、JavaScript、Rust 的相关 symbol；其他语言使用 bounded snippet/context hash。
- Git freshness 状态机识别 `fresh / moved / suspect / stale / unknown`，lazy + HEAD cache；refresh 只产生 replacement candidate，不改写原记忆。
- Retrieval 2.0 使用 FTS/vector/graph/temporal candidate union、RRF、freshness/scope/evidence filter、可选 top-N reranker 和 MMR，并持久化逐项 trace。
- Exact NumPy 是向量基线；`sqlite-vec` ANN 是可选 adapter，扩展缺失不会阻止启动。
- Context Compiler 按 task intent、coverage、truth/freshness、utility/cost 和预算选择最小证据集；未决冲突强制呈现双方。
- Consolidation 只生成带 `consolidated_from` lineage 的候选；反证产生 contested proposal，永不自动激活。
- helpful/unhelpful feedback 可审计，只影响 retrieval utility，不修改事实状态。
- 12 个 stdio MCP 工具、V2 HTTP API、CLI，以及 14 个页面的 Memory Intelligence Workbench。
- MemoryBench V2 覆盖 Extraction、Retrieval、Conflict、Temporal、Git、Consolidation、Context、Agent A/B harness 和真实 100k FTS5 P95。

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

发行形式为 PyInstaller `onedir`，必须保留整个 `release\MemoryOS` 目录。生产 smoke 会从真实 `0001_initial` 数据库启动，验证自动迁移到 `0002_memory_intelligence`、旧数据保留、12 个 MCP 工具、HTTP/UI/CLI、MemoryBench 资源和重启持久化。

## CLI 示例

```powershell
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data status --json
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data propose --repo my-repo --title "Use FastAPI" --content "Use FastAPI for the local API."
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data current-truth --query "backend framework"
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data debug-context "current backend constraints" --repo my-repo
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data consolidate --scope-key my-repo
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data refresh <memory-id> --repository-path C:\path\to\repo
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data backup --output .\backup.zip
```

运行 `python -m memoryos --help` 查看完整命令。

## MemoryBench 与验收

单独运行冻结评测：

```powershell
.\.venv\Scripts\python.exe scripts\memorybench_v2.py
```

输出：

- `docs/verification/v2/memorybench-report.json`
- `docs/verification/v2/memorybench-report.html`
- `docs/verification/v2/acceptance-summary.json`
- `docs/verification/v2/verify-summary.json`

真实模型与 fixture 严格分开：当前 30-task fixture 只验证 paired harness、指标和 bootstrap 95% CI。由于未配置真实 coding-agent endpoint，real-model Agent A/B 被如实记录为 `external_blocker`，项目不声称真实模型准确率或效果提升。

首次运行浏览器测试前安装 Chromium：

```powershell
Set-Location web
pnpm exec playwright install chromium
Set-Location ..
```

`scripts/verify.py` 依次执行 import、Ruff、format、Mypy strict、Pytest、MemoryBench、TypeScript、ESLint、Vitest、Vite build、Playwright、10k V1 性能回归、wheel、Windows package、V1→V2 packaged smoke 和 A15–A32 evidence manifest；任何一步失败即非零退出。

## 数据和隐私边界

- `memoryos.db`：SQLite WAL/FTS5 主事实库。
- `auth.token`：本地写 API token。
- `runtime.json`：最近一次服务地址。
- `logs/memoryos.log`：脱敏轮转日志。
- `backups/`：版本化备份；V2 可导入 V1，V1 不会静默读取 V2。

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
