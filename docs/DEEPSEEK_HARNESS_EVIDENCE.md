# MemoryOS × DeepSeek Harness：公开测试与工程证据

更新时间：2026-08-17

当前插件：`dsh-memoryos 0.1.18`

兼容基线：DeepSeek Harness `0.1.0-rc.5 @ 47f943859bef60e4160492346772ded9b24f765a`

## 当前结论

MemoryOS 的 DSH 集成已经证明插件机械链路、长期写入、硬重启回忆、Current Truth 更新、上下文淘汰后恢复、scope 隔离和逐请求计量可以真实工作。现有数据也出现过输入、费用、定位速度和补丁聚焦度改善的单题信号。

它尚未证明跨题的编程修复成功率提升，也未证明所有任务都会减少总 Token。简单且仓库自足的任务可能只有额外开销；完整上下文还在一个长会话中出现过错误锚定。公开结论因此保持为“可用且已有局部收益证据，泛化收益待更多冻结任务验证”。

## 测试矩阵

| 测试 | 对照与门 | 冻结结果 | 证据 |
|---|---|---|---|
| 全功能 A/B 验收 | `no_memory` 加 6 个 MemoryOS 模式；隐藏产物验证 | 14/14 隐藏验证通过，13/14 严格协议通过；Delta cold 一次被 Agent 跳过二次调用 | [全功能报告](../benchmarks/context_efficiency/full_plugin_acceptance_v1/full-acceptance-report.md) |
| 中等 A/B/C v1 | A 无记忆、B Full、C Progressive，同任务持续到通过或停止 | A、C 均通过；C 相比 A 输入 -16.20%、费用 -21.85%、补丁 -48.38%；B 到 298 次尝试仍未通过 | [当前报告](../benchmarks/context_efficiency/medium_abc_v1/continuation-current-report.md) |
| 中等 A/B/C v2 | 新题、并发三臂、130% 相对用量保护 | 三臂均未编辑；C 更早诊断但长推理使费用最高 | [v2 报告](../benchmarks/context_efficiency/medium_abc_v2/report-live-r2.md) |
| 中等 A/B/C v3 | 新题、同提示/起点/预算、插件侧通用压缩 | 三臂均未编辑；B 比 A 费用 -23.0%，C 仍发生长推理 | [v3 报告](../benchmarks/context_efficiency/medium_abc_v3/report-live-r1.md) |
| 中等成功校准 | Action-ready 自动展开，单臂验证“给对信息后能否执行” | 插件命中正确边界，但 Agent 继续查历史，18 次尝试后无补丁 | [校准报告](../benchmarks/context_efficiency/medium_success_calibration_v1/report-live-r1.md) |
| 真实跨 Session v1 | 3 个源会话、硬重启、每例 A 无记忆/B 同 scope/C 错 scope，共 12 个新会话 | 严格门 2/3；全部 B 回忆、A 弃答、C 隔离通过；数据库例源写入的两条英文语义被中文词法门判缺失 | [协议与结果](../benchmarks/context_efficiency/cross_session_memory_v1/README.md) |
| Memory Update | A 写 PG17，重启；B 更新到 PG18，重启；C 只问当前版本 | PASS；17 为 superseded，18 为 active，C 只答 18 | [后续验收](../benchmarks/context_efficiency/long_term_memory_followup_v1/README.md) |
| Context Eviction A/B | 同一连续会话，冻结小窗口和填充内容；末轮才允许回忆 | PASS；A 回答“不知道”，B 从 MemoryOS 恢复 `Glacier-47` | [后续验收](../benchmarks/context_efficiency/long_term_memory_followup_v1/README.md) |

单个 Requests 任务曾出现 MemoryOS 通过、基线失败；后续 MarkupSafe 和 Seaborn 配对均未通过，所以该结果保留为个例，不提升为成功率声明。

## 主要问题与解决方案

| 观察到的问题 | 根因 | 已实施的通用修复 |
|---|---|---|
| Full 模式长会话不收敛，B 消耗 54,003,040 输入 Token 后仍未通过 | 一次性完整记忆造成错误锚定，旧控制器又允许无限续跑 | 保留更小的 Progressive/Context-only 模式；增加与结束同伴相比的 130% 请求前保护；不把 Full 设为默认优胜方案 |
| 渐进模式偶发长推理 | 记忆已给出正确契约，但 Agent 继续查历史、上游和缺失依赖；推理被后续请求重复携带 | 压缩 schema/返回，单一 resolved 记录自动展开为 action-ready contract，并明确离线验证退路；仍如实记录该问题未被完全消除 |
| 隐藏验收一度能被 Agent 工作区读取 | 评分目录位于 Agent 权限根内 | 污染运行整体隔离，不进入结论；后续隐藏评分目录移到 Agent 权限根之外 |
| 冷/热请求哈希不稳定 | 哈希混入随机消息 ID、UI 元数据和一次性绝对工作区路径 | 只哈希 Provider 实际可见的 wire 投影，移除易变路径；补充真实 Loader 冷/热探针 |
| 跨会话写入发生候选/409 风暴 | 多个可独立变化的事实被合成一条；确认 Schema 没暴露冲突策略；结构化错误在桥接中丢失 | 写入要求稳定 semantic key 和单一原子事实；模型可见 `supersede/keep_both/reject`；保留错误 code/details；每会话锁住待解决冲突 |
| 更新后旧事实可能回来得“太好” | 检索与渲染没有充分保留稳定写入 key、原始已确认内容和 active-only 语义 | Context Atom 保留 `write_key` 与 `confirmed_memory`；更新测试检查 supersedes 链、旧记录状态和最终上下文 |
| 中文查询在 SQLite FTS5 `unicode61` 中召回不稳定 | 中文连续文本缺少适合的词边界 | 加入有 scope、状态、时态、行数和术语上限的 CJK n-gram/LIKE 后备检索，并保留负例隔离测试 |
| 1M 上下文问题无法证明“窗口外记忆” | 原消息可能仍在 Harness 当前 surface 中 | 增加仅评测启用的受控淘汰，记录被替换消息、sentinel hash 和保留面；最终回忆前验证 sentinel 已不在活动历史 |
| Linux CI 与 Windows 循环终止原因不同 | 工作区搜索强制依赖 `ripgrep`，缺失时同一只读调用变成失败调用 | 增加受限 Python 后备搜索；保持 2 MiB、结果数、目录、符号链接和二进制边界 |

## 最新长期记忆结果与 Token 分账

`long-term-memory-followup-v1` 的 live-r4 使用 `dsh-memoryos 0.1.18`、`deepseek-v4-flash`、0 次 Provider 重试。两项测试均通过。

| 写入会话 | `write_tool_schema_tokens` | `memory_write_visible_tokens` | `provider_input_tokens` |
|---|---:|---:|---:|
| Memory Update A | 598 | 2,593 | 34,061 |
| Memory Update B | 598 | 2,593 | 35,287 |
| Context Eviction MemoryOS A | 598 | 2,593 | 34,339 |
| 合计 | 1,794 | 7,779 | 103,687 |

前两列是冻结的 `unicode-heuristic-v1` 组件归因估算；第三列是 DeepSeek 返回的 Provider 精确输入用量。完整 campaign 为 24 次尝试、24 次完整响应、214,165 输入、3,743 输出、2,206 推理 Token，费用约 USD 0.0027042792。

## 过耦合控制

插件有意耦合 DSH 的 Cordis 生命周期、工具注册和 Provider 事件面，并锁定 RC5；这是适配器兼容约束，升级 DSH 时必须重做 Loader/HMR 与契约验收。MemoryOS 的 SQLite、迁移、检索、Truth、Context Compiler 和写入冲突处理仍在独立服务中，插件不复制这些逻辑。

为避免模型或题目特调，插件规则不包含 DeepSeek V4 Flash、pytest/Pylint、某个文件名、答案或“第六步必须写”等固定步数。评测专用写入与上下文淘汰必须显式开启；默认配置保持只读。对一个模型的效率信号不会自动改变 MemoryOS 核心检索权重或默认生产模式。

## 可复现入口

- 通用 Context Efficiency runner：[`benchmarks/context_efficiency/README.md`](../benchmarks/context_efficiency/README.md)
- 跨 Session runner：[`scripts/run_cross_session_memory_v1.py`](../scripts/run_cross_session_memory_v1.py)
- Update/Eviction runner：[`scripts/run_long_term_memory_followup_v1.py`](../scripts/run_long_term_memory_followup_v1.py)
- DSH Bundle：[`integrations/deepseek-harness-memoryos`](../integrations/deepseek-harness-memoryos)
- 独立公开插件：[`tianhao8687/dsh-memoryos`](https://github.com/tianhao8687/dsh-memoryos)

这些报告区分 Provider 精确计量、组件估算、失败运行、外部阻断和污染隔离。未经新的多任务冻结配对，不把单题方向性结果表述为普遍成功率提升。
