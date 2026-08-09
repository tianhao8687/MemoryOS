# MemoryOS

MemoryOS V1.0 是面向编码 Agent 的本地优先共享记忆层。它把决策、约束、失败经验、偏好和当前任务状态保存为带来源的结构化记忆，并通过同一个 SQLite 数据库向 MCP、HTTP API、CLI 和管理 UI 提供一致视图。

当前版本：`1.0.0`。完整验收命令为 `python scripts/verify.py`；验收项与证据映射见 [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)。

## 已实现

- 五级作用域：`user` / `workspace` / `repository` / `branch` / `task`。
- 六类记忆：`semantic` / `episodic` / `procedural` / `preference` / `project` / `working`。
- 可审计生命周期：`candidate` / `active` / `superseded` / `expired` / `forgotten` / `rejected`。
- Agent 和提取器默认只能提交 candidate；需要显式确认才能激活。
- 冲突检测与 `supersede` / `keep_both` / `reject` 解决策略。
- TTL / 有效期过期、逻辑遗忘、历史链、关系和完整审计记录。
- SQLite WAL + FTS5 离线基线；可选 OpenAI-compatible 提取器和 embedding 适配器。
- 具有作用域继承、去重、来源引用和预算限制的 context builder。
- 7 个真实 stdio MCP 工具、FastAPI HTTP API、Typer CLI。
- React 管理 UI：Overview、Projects、Memories、Candidates、Conflicts、Timeline、Audit、Settings。
- Git 仓库稳定身份、不存储源码的元数据集成。
- 带哈希和格式版本验证的备份/恢复及 JSONL 导入/导出。
- Windows PyInstaller 发行目录与洁净路径、重启持久化生产冒烟验证。

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

`serve` 默认在 `127.0.0.1` 上选择可用端口，并在终端打印 URL。未传 `--data-dir` 时，Windows 默认数据目录是 `%LOCALAPPDATA%\MemoryOS`；也可用 `MEMORYOS_HOME` 覆盖。

浏览器首次打开根页时会获得仅限本机同源写操作的 HttpOnly session cookie。外部 HTTP 写客户端需从 `<data-dir>\auth.token` 读取 bearer token。

## 运行 Windows 发行包

已生成的发行目录在 `release\MemoryOS`：

```powershell
.\release\MemoryOS\MemoryOS.exe --data-dir .\memoryos-data serve
```

命令会打开管理 UI。如不希望自动打开浏览器，添加 `--no-open`。发行包是 onedir 形式；运行时需保留整个 `MemoryOS` 目录，不要单独复制 EXE。

## CLI 常用操作

```powershell
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data status --json
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data doctor --json
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data propose --repo my-repo --title "Use FastAPI" --content "Use FastAPI for the local API."
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data search "FastAPI" --repo my-repo
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data list --status candidate
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data confirm <memory-id>
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data forget <memory-id>
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data backup --output .\backup.zip
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data export --output .\export.zip
.\.venv\Scripts\python.exe -m memoryos --data-dir .\restored import .\export.zip
```

可运行 `.\.venv\Scripts\python.exe -m memoryos --help` 查看完整参数。

## 一键验收

首次运行 E2E 前安装 Chromium：

```powershell
Set-Location web
pnpm exec playwright install chromium
Set-Location ..
```

然后在已安装 `.[dev]` 的 Python 环境中运行：

```powershell
.\.venv\Scripts\python.exe scripts\verify.py
```

该命令依次执行 14 个质量/产物门禁：导入、Ruff、格式、Mypy、Pytest、前端类型、ESLint、Vitest、Vite build、Playwright、10,000 条性能测试、wheel、Windows 打包和发行包生产冒烟。任一步失败即非零退出，汇总写入 `docs\verification\verify-summary.json`。

## 数据与运行边界

- `memoryos.db`：SQLite 主数据库；WAL、外键和 FTS5 在初始化时启用。
- `auth.token`：本地写 API 的随机 token。
- `runtime.json`：最近一次 `serve` 的 host/port。
- `logs\memoryos.log`：轮转日志，经密钥模式脱敏。
- `backups\`：默认备份位置。

MemoryOS 不自动扫描或收藏仓库源码；Git 集成只记录稳定仓库身份、路径、remote、branch 和 commit 元数据。来源 excerpt 在写入前脱敏并截断到配置上限。

## 文档

- [架构](ARCHITECTURE.md)
- [安全模型](SECURITY.md)
- [MCP 接入](MCP_SETUP.md)
- [验收证据](docs/ACCEPTANCE.md)
- [项目状态](PROJECT_STATUS.md)
- [实施决策](DECISIONS.md)
- [变更日志](CHANGELOG.md)
- [UI 设计系统](docs/DESIGN_SYSTEM.md)
