# MemoryOS MCP 接入

MemoryOS 提供本地 stdio MCP server。每个客户端可以启动独立子进程；只要所有配置使用同一个绝对 `data-dir`，它们就共享同一 SQLite 记忆库。

## 选择启动方式

### Windows 发行包（推荐日常使用）

- command：`C:\Users\Admin\Documents\ChatGPT\记忆模型\release\MemoryOS\MemoryOS.exe`
- args：`--data-dir`, `C:\Users\Admin\Documents\ChatGPT\记忆模型\memoryos-data`, `mcp`

保留整个 `release\MemoryOS` onedir 发行目录；不要单独移动 EXE。

### 源码环境（推荐开发）

- command：`C:\Users\Admin\Documents\ChatGPT\记忆模型\.venv\Scripts\python.exe`
- args：`-m`, `memoryos.mcp_server.server`, `--data-dir`, `C:\Users\Admin\Documents\ChatGPT\记忆模型\memoryos-data`
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
args = ["--data-dir", "C:\\Users\\Admin\\Documents\\ChatGPT\\记忆模型\\memoryos-data", "mcp"]
startup_timeout_sec = 20
tool_timeout_sec = 60
required = true
```

源码模式的等价配置：

```toml
[mcp_servers.memoryos]
command = "C:\\Users\\Admin\\Documents\\ChatGPT\\记忆模型\\.venv\\Scripts\\python.exe"
args = ["-m", "memoryos.mcp_server.server", "--data-dir", "C:\\Users\\Admin\\Documents\\ChatGPT\\记忆模型\\memoryos-data"]
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
        "mcp"
      ]
    }
  }
}
```

## Claude Code 配置模板

以下命令是通用 stdio 模板；Claude Code CLI 版本可能调整参数，使用时应以当前客户端的 `claude mcp --help` 为准。本项目的自动验收没有操作 Claude Code UI。

```powershell
claude mcp add --transport stdio memoryos -- "C:\Users\Admin\Documents\ChatGPT\记忆模型\release\MemoryOS\MemoryOS.exe" --data-dir "C:\Users\Admin\Documents\ChatGPT\记忆模型\memoryos-data" mcp
```

## 工具清单

| 工具 | 用途 | 写数据 |
| --- | --- | --- |
| `memory_context` | 按 task/repo/branch/workspace/task scope 生成带来源的结构化 context | 否 |
| `memory_search` | FTS5/可选混合检索，支持 scope/type/status/history 过滤 | 否 |
| `memory_propose` | 提交 source-backed candidate；Agent 不能直接写 active | 是 |
| `memory_confirm` | 确认 candidate；冲突时需显式 strategy 和可选 rationale | 是 |
| `memory_forget` | 逻辑忘却，保留最小 provenance/audit | 是 |
| `memory_history` | 查询 memory ID 或 semantic key 的状态/替代历史 | 否 |
| `memory_explain` | 返回来源、hash、作用域、创建者、状态、关系和审计 | 否 |

## 建议的 Agent 工作流

1. 开始任务时调用 `memory_context`，传入当前 repo 和 branch。
2. 不确定时用 `memory_search`；需要可信理由时用 `memory_explain`。
3. 仅对稳定、可复用且有明确来源的信息调用 `memory_propose`。
4. 让用户或有明确授权的工作流调用 `memory_confirm`；不要自动解决语义冲突。
5. 信息过时时用 `memory_forget` 或通过新 candidate 建立 supersession 链，不直接删库。

## 连接验证

以下自动测试使用官方 Python MCP client 启动真实 stdio 子进程，列出 7 个工具，完成 propose → confirm → context，关闭 MCP 后再用 HTTP 读取同一条记忆：

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
