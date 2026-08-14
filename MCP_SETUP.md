# MemoryOS MCP 接入

MemoryOS 提供本地 stdio MCP server。每个客户端可以启动独立子进程；只要所有配置使用同一个绝对 `data-dir`，它们就共享同一 SQLite 记忆库。

V2.3 的工具配置档和 MSC 新参数已在源码中可用。如本地 `release\MemoryOS` 仍是 V2.2
包，请先在合并后的干净 `main` 上重建；不要把旧包当成 V2.3。

## 选择启动方式

### Windows 发行包（推荐日常使用）

- command：`C:\Users\Admin\Documents\ChatGPT\记忆模型\release\MemoryOS\MemoryOS.exe`
- args：`--data-dir`, `C:\Users\Admin\Documents\ChatGPT\记忆模型\memoryos-data`, `mcp`, `--tool-profile`, `all`

保留整个 `release\MemoryOS` onedir 发行目录；不要单独移动 EXE。

### 源码环境（推荐开发）

- command：`C:\Users\Admin\Documents\ChatGPT\记忆模型\.venv\Scripts\python.exe`
- args：`-m`, `memoryos.mcp_server.server`, `--data-dir`, `C:\Users\Admin\Documents\ChatGPT\记忆模型\memoryos-data`, `--tool-profile`, `all`
- cwd：`C:\Users\Admin\Documents\ChatGPT\记忆模型`

`data-dir` 应使用稳定绝对路径，不要依赖客户端的当前工作目录。

## Codex / ChatGPT desktop / IDE extension

根据 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)，同一 Codex host 上的 ChatGPT desktop app、Codex CLI 和 IDE extension 共享 MCP 配置。可直接用 CLI 添加已打包 server：

```powershell
codex mcp add memoryos -- "C:\Users\Admin\Documents\ChatGPT\记忆模型\release\MemoryOS\MemoryOS.exe" --data-dir "C:\Users\Admin\Documents\ChatGPT\记忆模型\memoryos-data" mcp
codex mcp list
```

或在用户级 `~/.codex/config.toml` 或受信任项目的 `.codex/config.toml` 中添加：

```toml
[mcp_servers.memoryos]
command = "C:\\Users\\Admin\\Documents\\ChatGPT\\记忆模型\\release\\MemoryOS\\MemoryOS.exe"
args = ["--data-dir", "C:\\Users\\Admin\\Documents\\ChatGPT\\记忆模型\\memoryos-data", "mcp", "--tool-profile", "all"]
startup_timeout_sec = 20
tool_timeout_sec = 60
required = true
```

源码模式的等价配置：

```toml
[mcp_servers.memoryos]
command = "C:\\Users\\Admin\\Documents\\ChatGPT\\记忆模型\\.venv\\Scripts\\python.exe"
args = ["-m", "memoryos.mcp_server.server", "--data-dir", "C:\\Users\\Admin\\Documents\\ChatGPT\\记忆模型\\memoryos-data", "--tool-profile", "all"]
cwd = "C:\\Users\\Admin\\Documents\\ChatGPT\\记忆模型"
startup_timeout_sec = 20
tool_timeout_sec = 60
required = true
```

在 ChatGPT desktop app 或 IDE extension 中，也可在 Settings → MCP servers 选择 STDIO，然后填入上述 command 和 args，保存后按客户端提示重启。用 `/mcp` 或 `codex mcp list` 检查连接。

## Cursor 配置模板

以下是通用 stdio `mcp.json` 模板；它展示了 MemoryOS 的正确命令行，但本项目的自动验收没有操作 Cursor UI。

```json
{
  "mcpServers": {
    "memoryos": {
      "command": "C:\\Users\\Admin\\Documents\\ChatGPT\\记忆模型\\release\\MemoryOS\\MemoryOS.exe",
      "args": [
        "--data-dir",
        "C:\\Users\\Admin\\Documents\\ChatGPT\\记忆模型\\memoryos-data",
        "mcp",
        "--tool-profile",
        "all"
      ]
    }
  }
}
```

## Claude Code 配置模板

以下命令是通用 stdio 模板；Claude Code CLI 版本可能调整参数，使用时应以当前客户端的 `claude mcp --help` 为准。本项目的自动验收没有操作 Claude Code UI。

```powershell
claude mcp add --transport stdio memoryos -- "C:\Users\Admin\Documents\ChatGPT\记忆模型\release\MemoryOS\MemoryOS.exe" --data-dir "C:\Users\Admin\Documents\ChatGPT\记忆模型\memoryos-data" mcp --tool-profile all
```

## 启动时工具配置档

配置档在进程启动时固定；任何工具调用都不会修改当前连接的 tools/list。`all` 仍是兼容默认。
未传 `--tool-profile` 时读取 `MEMORYOS_MCP_TOOL_PROFILE`，该环境变量未设置时才回到 `all`；
显式命令行参数优先。

| Profile | 工具数 | 工具 |
| --- | ---: | --- |
| `all` | 12 | 完整兼容集 |
| `core` | 6 | `context`、`search`、`propose`、`confirm`、`explain`、`current_truth` |
| `governance` | 4 | `forget`、`feedback`、`consolidate`、`refresh` |
| `debug` | 2 | `history`、`debug_context` |

源码启动示例：

```powershell
.\.venv\Scripts\python.exe -m memoryos.mcp_server.server --data-dir .\memoryos-data --tool-profile core
.\.venv\Scripts\python.exe -m memoryos.mcp_server.server --data-dir .\memoryos-data --tool-profile governance
.\.venv\Scripts\python.exe -m memoryos.mcp_server.server --data-dir .\memoryos-data --tool-profile debug
```

支持多 Namespace/延迟加载的客户端可把它们配成三个使用同一 `data-dir` 的 server：

```text
memoryos_core        -> --tool-profile core        # 常驻
memoryos_governance  -> --tool-profile governance  # defer loading
memoryos_debug       -> --tool-profile debug       # defer loading
```

具体 `defer_loading`/Tool Search 字段由客户端和模型 API 定义，配置时应重新核对当前客户端文档。
服务端 Profile 只保证暴露的 Schema 变小；它不保证 Provider 实际 input token 必然减少。
评测保存每个 Profile 的规范 Schema Snapshot、SHA-256 和带 counter 标识的估算，但端到端结论
仍以 Provider Usage 为准。

## 工具清单

| 工具 | 用途 | 写数据 |
| --- | --- | --- |
| `memory_context` | 按 task/repo/branch/workspace/task scope 生成 context；V2.3 MSC 可使用 Token Profile、显式 Delta 和瘦响应 | 否 |
| `memory_search` | FTS5/可选混合检索，支持 scope/type/status/history 过滤 | 否 |
| `memory_propose` | 提交 source-backed candidate；Agent 不能直接写 active | 是 |
| `memory_confirm` | 确认 candidate；冲突时需显式 strategy 和可选 rationale | 是 |
| `memory_forget` | 逻辑忘却，保留最小 provenance/audit | 是 |
| `memory_history` | 查询 memory ID 或 semantic key 的状态/替代历史 | 否 |
| `memory_explain` | 兼容返回来源和审计；可用 expected Atom hash 按需展开 fact/evidence/freshness/relations/history | 否 |
| `memory_current_truth` | 按 entity/predicate 或自然语言查询 resolved/contested/stale truth | 否 |
| `memory_feedback` | 对指定 retrieval run 中的 memory 提交 helpful/unhelpful 反馈 | 是 |
| `memory_consolidate` | 扫描 episodic pattern；默认 dry-run，只生成候选 | 可选 |
| `memory_refresh` | 重算 Git anchor freshness，并可生成 replacement candidate | 是 |
| `memory_debug_context` | 按 `retrieval_run_id` 读取已保存的 query plan、RRF/rerank、manifest、预算和 Shadow 诊断 | 否 |

## 建议的 Agent 工作流

1. 开始任务时调用 `memory_context`，传入当前 repo 和 branch；MSC 后续轮次显式传上一个 `context_id`。
2. 不确定时用 `memory_search`；查询当前结论时用 `memory_current_truth`；需要可信理由时用 FACT 行里的 `memory_id` 和 `atom_sha256` 调用 `memory_explain`。
3. 仅对稳定、可复用且有明确来源的信息调用 `memory_propose`。
4. 让用户或有明确授权的工作流调用 `memory_confirm`；不要自动解决语义冲突。
5. 代码证据可能过时时先用 `memory_refresh`；它生成候选而不会静默改原结论。
6. 只对本次 retrieval 中实际使用过的 memory 调用 `memory_feedback`；反馈不是真相编辑。
7. 调试 MSC 时传 `retrieval_run_id` 读已保存诊断，不重跑一次可能变化的检索。
8. 定期用 `memory_consolidate` 预览重复 episode，确认反证和 lineage 后再人工激活候选。

## 连接验证

以下自动测试使用官方 Python MCP client 启动真实 stdio 子进程，列出 12 个工具，完成 propose → confirm → context，关闭 MCP 后再用 HTTP 读取同一条记忆：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_mcp_stdio.py
```

打包后链路由以下命令验证：

```powershell
.\.venv\Scripts\python.exe scripts\production_smoke.py --distribution release\MemoryOS --output docs\verification\package-smoke.json
```

## 故障排查

- 客户端显示 server 立即退出：先在 PowerShell 中直接运行同一 command/args，并检查绝对路径。
- 不同客户端看到不同数据：对比每个配置的 `--data-dir`；它们必须完全相同。
- 工具列表不完整：重启客户端，再查看 `/mcp` 或客户端 MCP 日志。
- 不要向 stdout 中间层添加调试输出；stdio 的 stdout 是 JSON-RPC 协议通道。MemoryOS 应用日志位于 `<data-dir>\logs\memoryos.log`。
