# MemoryOS V2.3 插件全功能 A/B 验收

生成时间：2026-08-16T11:59:10Z  
模型：`deepseek-v4-flash`  
插件：`dsh-memoryos 0.1.6`  
Harness：`0.1.0-rc.5 @ 47f943859bef60e4160492346772ded9b24f765a`

## 结论

总判定：**有条件通过（conditional pass）**。

- 所有插件能力都至少真实执行成功过一次：关闭插件、Legacy Full、MSC Full、渐进披露、`memory_explain`、Delta、Delta Core、Compact Context、精确用量、缓存拆分、安全隔离、安装和 Loader/HMR。
- 主验收 14 个运行的隐藏验证全部通过；严格条件协议为 13/14。
- 唯一协议失败是 `msc_delta/cold`：Agent 完成了正确产物，但只调用了一次 `memory_context`，没有执行第二次 Delta 调用。插件的 Delta 本身已在 `msc_delta/warm` 和 `msc_delta_core` 冷/热三次成功命中。
- 这次 A/B **没有证明成功率提高**：任务对 A、B 都能完成。它证明的是插件功能可用、安全边界有效、计量可信，以及各模式的开销。

## 为什么这不是“修改代码特调”

验收任务不是修代码，而是读取仓库中的发布政策和当前指标，生成并验证一个发布决策 JSON。任务提示不包含 MemoryOS、插件名或工具名；A/B 使用相同模型、运行时、起始提交、任务文本和隐藏验证。

需要明确一个限制：初始完整运行仍把各条件的一次性绝对工作区路径写入 headless 全局 persona，因此旧运行的 system prompt 不是逐字节相同。修复后的最终探针已证明 A/B 的 system、messages、generation 完全相同，只有预期中的 tools 组件不同。所以下面的旧运行开销适合作为功能验收和方向性数据，不能当作高精度因果效果估计。

DeepSeek 优化预设确实是模型侧配置，但 A/B 两边完全相同，因此不是插件效果的混杂变量。插件仍有一处有意的适配器耦合：它锁定 Harness `0.1.0-rc.5`，升级 Harness 时必须重新做兼容验收。

## 主验收汇总

| 指标 | 结果 |
|---|---:|
| 主运行数 | 14 |
| 隐藏验证通过 | 14/14 |
| 严格条件协议通过 | 13/14 |
| 实际服务器请求 | 97 |
| 重试 | 0 |
| 输入 token | 949,383 |
| 其中缓存 miss / hit | 144,519 / 804,864 |
| 输出 token | 24,354 |
| 精确费用 | $0.0293053992 |
| 过期记忆使用 | 0 |
| 跨项目泄漏 | 0 |
| 触发请求上限 | 0 |

所有主运行都在 5–8 个请求内结束，绝对上限为 10。

## 功能覆盖

| 功能 | 判定 | 真实证据 |
|---|---|---|
| `no_memory` 基线 | 通过 | 不暴露内存工具，内存调用始终为 0 |
| Legacy Full | 通过 | 冷/热各完成一次 Full 调用 |
| MSC Full | 通过 | 冷/热各完成一次 Full 调用 |
| Progressive + Explain | 通过 | 冷阶段调用 context + explain；热阶段只取 context，证明按需展开 |
| Delta | 有条件通过 | 三次 Delta 命中；`msc_delta/cold` 的 Agent 跳过第二次调用 |
| Delta Core | 通过 | 冷/热均完成第二次调用并命中 Delta |
| Compact Context | 通过 | 参数极简的 `memory_context` 完成非代码任务 |
| 过期/跨项目隔离 | 通过 | 两个 canary 均未被使用或泄漏 |
| Provider 精确计量 | 通过 | 每个完成请求都有 input、cache miss/hit、output、latency、hash、cost |
| 安装与 Loader/HMR | 通过 | 新 Harness home 安装成功；工具可禁用/恢复；usage 始终挂载 |
| 冷/热请求稳定性 | 修复后通过 | A/B 两组的首请求 hash、bytes、四类组件均一致 |

## A/B 开销

下表均相对同阶段 `no_memory`。这是单任务功能验收，而且完整运行发生在全局 persona 路径修复之前；这些差值只能作方向性参考，负数不能解释为已证明的优化收益。

| 条件 | 阶段 | 输入 token 变化 | 请求变化 | 延迟变化 | 费用变化 |
|---|---|---:|---:|---:|---:|
| Legacy Full | cold | +22.01% | 0 | +2.361s | +$0.0004626776 |
| Legacy Full | warm | +16.79% | 0 | +1.408s | +$0.0003181248 |
| MSC Full | cold | +5.73% | 0 | +4.357s | +$0.0002079000 |
| MSC Full | warm | -0.80% | 0 | -0.311s | -$0.0000508536 |
| Progressive | cold | +10.40% | 0 | +7.051s | +$0.0003707928 |
| Progressive | warm | +0.20% | 0 | +1.424s | +$0.0000039984 |
| Delta | cold | +4.31% | 0 | +2.381s | +$0.0001009344 |
| Delta | warm | +19.72% | +1 | +3.561s | +$0.0002296448 |
| Delta Core | cold | +26.56% | +1 | +7.769s | +$0.0003833984 |
| Delta Core | warm | +3.57% | 0 | +3.116s | +$0.0001585360 |
| Compact Context | cold | +25.16% | +1 | +5.090s | +$0.0002555672 |

Compact 完整任务多了 25.16% 输入 token，但最终首请求探针显示，单纯暴露插件只增加 43 个输入 token（约 0.55%）。主要开销不是一大段固定提示，而是取回记忆后多进行了一轮模型请求。这一轮在需要外部记忆时有价值，在仓库自身已足够解决的简单任务上则只是开销。

## 缓存证据修复

原计量把 Harness 随机消息 ID、UI/source 元数据也加入请求哈希，同时 headless 全局 persona 仍包含一次性绝对工作区路径。这些字段导致“模型实际等价”的冷/热请求被误判为不同。

修复后：

- 哈希改为 DeepSeek 供应商实际可见的 wire 投影；不记录原始提示内容。
- headless 全局 persona 不再包含一次性工作区路径。
- 离线真实 Loader 探针的 system/messages/tools/generation 四个组件全部一致，外部 API 请求为 0。
- 最终在线补充验收实际完成 4 个服务器请求、0 重试、4 条 provider-exact 记录，费用 $0.0003152128。
- `no_memory` 冷/热首请求：同一 SHA-256，33,476 bytes。
- `msc_context_only` 冷/热首请求：同一 SHA-256，33,677 bytes；冷/热各成功调用一次 `memory_context`，基线为 0。
- 修复后的 A/B 首请求中 system、messages、generation 三部分相同；唯一差异是应有的 tools 组件（基线 27,063 bytes，插件组 27,264 bytes）。

本轮 nominal cold 和 warm 都已有很高的 provider cache hit，因为此前相同内容的诊断请求已经填充内容寻址缓存。因此可以确认“请求稳定且缓存拆分计量准确”，但不能用这四次请求估计纯净 cold→warm 的命中率提升。

## 请求与费用审计

| 来源 | 完成的服务器请求 |
|---|---:|
| 主验收 | 97 |
| 早期缓存 v2（任务完成，但缓存哈希门无效） | 24 |
| 最终缓存 v4 | 4 |
| 其他挂载失败、离线探针、pre-dispatch 探针 | 0 |
| **合计** | **125** |

所有验收产物合计输入 1,202,747 token、输出 30,418 token、精确费用 $0.0370234760。最终修复之后没有继续重复完整任务。

## 发布建议

可以把插件 `0.1.6` 作为**按需启用的有条件功能版本**，但不能宣称它已提高任务成功率。

- 一般记忆相关任务优先 `msc_full`：本轮有效 Full 模式里开销最平衡。
- 需要追溯证据时使用 Progressive。
- 必须保证二次 Delta 调用时使用 `msc_delta_core`；暂时不要依赖 Agent 自觉完成自由式 `msc_delta` 协议。
- 对仓库信息已经自足的简单任务保持插件关闭。

## 本地验证

- Python：33 passed（另有 1 条与本改动无关的 Pydantic warning）
- Node 插件与真实 Loader/HMR：8 passed
- Ruff：passed
- 主验收隐藏验证：14/14 passed

机器可读结果见 `full-acceptance-report.json`；冻结配置见 `acceptance-lock.json`、`cache-prefix-validation-lock-v3.json` 和 `cache-prefix-validation-lock-v4.json`。
