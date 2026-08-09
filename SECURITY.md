# MemoryOS 安全模型

## 信任边界

MemoryOS V1 是单用户、单机、loopback-only 工具。它信任当前操作系统用户和用该用户身份启动的 MCP 客户端，不把本机其他用户、未信任网页、LAN 设备或外部 provider 视为可信边界。

安全目标：

- 不向网络暴露本地数据服务。
- 防止普通网页无凭据改写记忆。
- 保留所有重要状态转换的来源和审计链。
- 在持久化和日志之前删除常见密钥形态。
- 不将仓库源码隐式复制到记忆库。
- 在恢复或导入前验证档案结构和内容完整性。

## 已实现控制

### 网络与身份验证

- `host` 配置只接受 `localhost`、`127.0.0.0/8` 或 `::1`；`0.0.0.0`、LAN IP 和任意主机名在设置校验时被拒绝。
- HTTP 读端点不要求身份验证，因此依赖 loopback 和操作系统账号边界。
- 所有 HTTP 写端点需要随机 48-byte URL-safe token。外部客户端使用 `Authorization: Bearer <token>`。
- 管理 UI 从根页获得 `HttpOnly` + `SameSite=Strict` cookie，仅用于同源本机写操作。因为 V1 使用 localhost HTTP，cookie 没有 `Secure` 标志。
- 写请求还校验 `Origin`；仅接受 localhost/loopback origin。CORS 是浏览器边界，不替代 token 校验。
- MCP 使用 stdio，不打开网络端口；其权限等同于启动它的本机客户端进程。

`auth.token` 创建后会尝试设为 `0600`。Windows 上最终隔离仍由数据目录的 NTFS ACL 决定；不应将 data directory 放在公共或多用户可写目录。

### 输入、密钥与日志

- Pydantic 请求模型拒绝额外字段，并对字符串、枚举、数值范围和 TTL 设上限。
- 正文、来源 excerpt 和 MemoryOS 日志都会识别并替换 OpenAI key、GitHub token、AWS access key、Bearer token 及常见 password/secret 赋值形态。
- 来源 excerpt 持久化默认不超过 2,000 字符；来源同时保存脱敏内容的 SHA-256。
- 日志保存在 `<data-dir>/logs/memoryos.log`，每行为带 UTC timestamp/level/logger/message 的 JSON；2 MiB 轮转，保留 3 个旧文件。日志过滤器在 JSON 格式化前执行脱敏。
- 默认没有遥测、分析 SDK 或云同步。

模式脱敏只能降低常见凭据泄漏风险，不是通用数据防泄漏系统。在写入之前仍应尽量不向 MemoryOS 提交秘密。

### 数据完整性与恢复

- SQLite 开启 foreign keys、WAL、busy timeout 和 `synchronous=NORMAL`。
- 备份 ZIP 只允许 `manifest.json` + `memoryos.db`；恢复前校验格式版本、SHA-256、SQLite `integrity_check` 和必需表。
- 恢复默认先对当前数据库创建 safety backup。
- JSONL 交换 ZIP 只允许 `manifest.json` + `data.jsonl`，并校验格式版本、payload hash、record type 和基本 schema。
- 逻辑忘却只更改状态，不删除 provenance 或 audit；历史仍可解释。

### 供应商和出站数据

FTS5 和 heuristic extractor 是默认离线路径。只有设置 `MEMORYOS_EMBEDDING_*` 或 `MEMORYOS_EXTRACTOR_*` 时才会调用外部 OpenAI-compatible endpoint。配置这些选项等于明确授权向该 provider 发送相关查询或提取文本；应自行评估 provider 的数据政策。

Provider 返回非法 JSON 时请求失败且不写入数据库；embedding 不可用时检索会回退到 FTS5。

## 源码最小化

Git 检测仅执行元数据命令：仓库根、当前 branch/HEAD 和 origin URL。稳定身份由规范化 remote 的哈希生成；无 remote 时使用本地 marker。集成不读取、分块、embedding 或存储源文件内容。

## 已知边界

- SQLite 文件、备份和 token 不做应用层静态加密；依赖 BitLocker/EFS 等操作系统加密和 NTFS ACL。
- `sensitivity=sensitive` 是可审计标记，V1 不会据此建立独立密钥或行级权限。
- 读 API 对本机进程开放；不适用于不信任的共享主机。
- 导入档案有结构与哈希校验，但 V1 没有对压缩包大小做配额控制；不应导入不可信的超大 ZIP。
- 该服务没有为网络或多用户部署设计；不应通过反向代理暴露。

## 运行检查

```powershell
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data doctor --json
```

`doctor` 检查数据库完整性、FTS5、token、loopback bind、数据目录、UI 产物和 embedding provider 状态。不配置 embedding 时的 `WARN` 表示 FTS5 fallback 正常启用，不是完整性失败。
