# MemoryOS 安全模型

## 信任边界

MemoryOS V2.1 是单用户、单机、loopback-only 工具。它信任当前操作系统用户和用该用户身份启动的 MCP 客户端，不把本机其他用户、未信任网页、LAN 设备或外部 provider 视为可信边界。

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
- 纯只读、无状态 HTTP 端点依赖 loopback 和操作系统账号边界。会触发到期转换、检索审计或 health 计数的状态型读取（例如 memories search、context、current truth、status）也要求 token，避免匿名读取间接改变治理状态。
- 所有 HTTP 写端点和状态型读取需要随机 48-byte URL-safe token。外部客户端使用 `Authorization: Bearer <token>`。
- 管理 UI 从根页获得 `HttpOnly` + `SameSite=Strict` cookie，仅用于同源本机写操作。因为 V2 使用 localhost HTTP，cookie 没有 `Secure` 标志。
- 受保护请求还校验精确 `Origin`（scheme + host + port）；默认只生成当前绑定端口对应的 loopback origin。额外配置也必须是带显式端口的 loopback URL，不把无端口或任意 localhost 端口视为同源。CORS 是浏览器边界，不替代 token 校验。
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
- 恢复默认先对当前数据库创建 safety backup。外来数据库先在隔离目录中迁移，并与当前版本现场生成的表、列、索引、约束、外键和 FTS 签名比对；只有通过后才原子替换在线库，激活失败会自动回滚。
- ZIP 导入拒绝重复/额外条目，并在解压前检查压缩包、manifest、数据库/JSONL entry 与记录数量上限，避免无限制解压和内存分配。
- JSONL 交换 ZIP 只允许 `manifest.json` + `data.jsonl`，并校验格式版本、payload hash、record type、非有限数、向量维度和数据库完整性；导入错误会回滚整个事务。
- SQLite 恢复和 JSONL 导入都会使持久化 ANN 缓存失效，避免备份中的 embedding 与本机旧索引恰好同数量时产生静默错配；下次语义查询会从数据库重建。
- 逻辑忘却和 archive 只追加状态版本，不删除 provenance 或 audit；archive 可恢复，历史仍可解释。
- 归档保护只把当前 valid-time 有效、未 stale、未归档且仍 active 的 alternative 计作替代支持，因此未来事实不能用来移除当下唯一的 accepted current truth；distillation 只能读取 Cold/Archived 输入且输出 candidate。

### 供应商和出站数据

FTS5 和 heuristic extractor 是默认离线路径。只有设置 `MEMORYOS_EMBEDDING_*` 或 `MEMORYOS_EXTRACTOR_*` 时才会调用外部 OpenAI-compatible endpoint。配置这些选项等于明确授权向该 provider 发送相关查询或提取文本；应自行评估 provider 的数据政策。

Extractor/relationship provider 返回非法 JSON 时请求失败且不写入候选或真值。Embedding 是 active memory 提交后的可选索引步骤：畸形、空、非有限或维度异常的向量会被拒绝，记忆写入仍成功并继续使用 FTS5；ANN 初始化、写入或查询中途失效时会显式回退到 exact NumPy/FTS5，不把故障伪装成空结果。

Relationship judge 不能自由扫描数据库。确定性规则先分类，只有不确定 pair 会发送 bounded claim 和 evidence；完整 prompt 不进入日志。Possible Conflict 只保存最小判断结果、provider fingerprint、prompt version、evidence hash 和 resolution audit。模型失败或 abstain 不得修改 accepted truth。

CodingMemoryBench 的 runtime payload 与 gold labels 分开构造并分别哈希，gold 只由 scorer 加载。报告中的确定性 fixture 明确为 harness-only；没有 endpoint/credentials 时输出 `external_blocker` 与 `effect_claim=none`，不伪造真实模型效果。

## 源码最小化

仓库发现仅执行元数据命令：仓库根、当前 branch/HEAD 和 origin URL。持久化前会删除 remote 中的 userinfo、query 和 fragment，稳定身份由规范化 remote 的哈希生成；无 remote 时使用本地 marker。V2 Source Anchor 只在用户或调用方明确给出 `path`/`symbol_fqn` 后读取该文件，并仅保存 bounded excerpt、hash、symbol 与 commit 元数据；创建和刷新（包括导入的 anchor）都在解析 symlink 后重新验证路径仍在仓库内，不会扫描、分块、embedding 或收藏整个仓库源码。

## 已知边界

### V2.2 real-workload 回放边界

V2.2 回放只在宿主机执行 Git 元数据、净化 checkout 和补丁应用；第三方仓库命令与隐藏测试不得在宿主机运行。代理容器和 MCP sidecar 使用非 root 用户、只读 rootfs、`cap-drop ALL`、`no-new-privileges` 及 CPU/内存/PID 限制。隐藏测试固定 `--network none`；future solution object、隐藏 overlay、memory seed/SQLite 都不挂载给代理。确认性模式禁止代理拥有不受限互联网出口，以免从远端取回 solution。

代理只挂载一个宿主预创建的结构化结果文件，不挂载日志目录；stdout/stderr 由宿主在挂载边界外限长保存。代理返回后，宿主 Git 操作前会校验 `.git` 是真实目录、config/hooks/info 未变化、无 object alternates、链接或特殊文件；随后禁用 system/global Git config、hooks、external diff 与 textconv，并从固定 base commit 捕获补丁。因此代理自行 commit 不会逃逸评分，篡改 Git clean filter 或 hook 也不会在宿主机执行。运行时必须显式区分 `deterministic_fixture` 与 `real_coding_agent`；前者无论样本量都不能产生效果声明。

`build/real-workload/run-state/` 可能包含原始日志、补丁或记忆，不属于可发布证据；公开前只使用经过脱敏检查的 `docs/verification/v2.2/<run-id>/` 报告。确定性 fixture 只验证基础设施，不是模型效果证据。

- SQLite 文件、备份和 token 不做应用层静态加密；依赖 BitLocker/EFS 等操作系统加密和 NTFS ACL。
- `sensitivity=sensitive` 是可审计标记，V2 不会据此建立独立密钥或行级权限。
- 读 API 对本机进程开放；不适用于不信任的共享主机。
- 导入配额是本地单用户产品的资源保护，不是恶意多租户隔离；仍不应导入来源不明的档案。
- 该服务没有为网络或多用户部署设计；不应通过反向代理暴露。

## 运行检查

```powershell
.\.venv\Scripts\python.exe -m memoryos --data-dir .\data doctor --json
```

`doctor` 检查数据库完整性、FTS5、token、loopback bind、数据目录、UI、embedding provider、sqlite-vec runtime 与 namespace 状态。不配置 embedding 时的 `WARN` 表示 FTS5 fallback 正常启用，不是完整性失败。
