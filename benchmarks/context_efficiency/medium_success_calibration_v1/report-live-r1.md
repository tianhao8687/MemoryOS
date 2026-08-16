# DeepSeek V4 Flash 中等题成功校准报告（r1）

## 结论

这轮没有通过验收。MemoryOS 插件 **调用成功、自动展开成功、信息也命中了正确代码边界**，但 Agent 没有从“已经知道怎么改”切换到编辑；它在 18 次 provider 尝试的无补丁上限处被停止，最终补丁为空。

因此当前只能得出两个结论：

1. 插件 0.1.10 的 action-ready 快速路径在真实 Harness 中确实生效；
2. 它还不足以让 DeepSeek V4 Flash 在这道题上完成修复，不能据此声称插件提高了成功率。

这不是 MemoryOS 调用失败、联网失败、依赖缺失或题目环境不可复现。主要问题是 **正确定位之后仍继续调查，没有进入实现**。

## 冻结结果

| 指标 | 结果 |
| --- | ---: |
| 条件 | `msc_progressive` |
| 模型 | `deepseek-v4-flash` |
| 插件 | `0.1.10` |
| provider 尝试 | 18 |
| 完整响应 | 17 |
| 输入 Token | 274,147 |
| 缓存命中 / 未命中 | 249,728 / 24,419 |
| 输出 Token | 11,524 |
| 推理 Token | 9,729 |
| 已返回 usage 的成本 | $0.0073446184 |
| Agent 延迟 | 154.08 秒 |
| 补丁 | 0 bytes |
| Agent 测试 | 0 |
| 隐藏验收 | 失败（空补丁） |
| 停止原因 | `provider_attempts=18/18` 无补丁上限 |

输入、输出和成本来自 provider 对 17 个完整响应返回的 usage。第 18 次请求在响应完成前被控制器停止，没有 usage 记录，因此账面成本不应被解释成供应商最终账单的严格上界。

## 插件到底有没有工作

有，机械链路是完整的：

- 模型只调用了一次 `memory_context`；
- 插件在本地自动调用了一次后端 `memory_explain`，模型没有再发起第二次 MemoryOS 工具调用；
- 第一步就返回了 `status=resolved`、`readiness=ready_to_implement`、`external_lookup_required=false`；
- 最终模型可见 contract 为 1,404 个字符、约 351 个估算 Token；
- contract 明确指出 `PyobjMixin.obj` 是懒加载边界，`Package.collect` 不应在目录遍历时强制挂载对象。

模型随后也证明它理解了这段记忆：读到 `src/_pytest/python.py` 后，它准确识别了 `Package.collect()` 开头的 `_mount_obj_if_needed()` 会提前导入 `__init__.py`，并明确说出了最小修复方向。

所以问题不是“插件没被调用”或“memory_context 没给出东西”，而是 **给对以后没有形成执行闭环**。

## 为什么仍然失败

完整轨迹共有 18 个工具调用：`memory_context` 1 次、`bash` 15 次、`read` 2 次，编辑和测试都是 0 次。其中 9 次调用用于 Git 历史或 CHANGELOG，8 次成功返回历史材料。

关键过程是：

1. 第 1 步取得 action-ready contract；
2. 第 3 步已经根据记忆说出 eager mount 是回归来源；
3. 第 9–12 步定位并读取 `Package.collect` 与 `PyobjMixin.obj`；
4. 第 13–14 步仍用 `git log -S`、`git log -L`、`git show` 重建历史修改；
5. 第 15 步再次得出应删除 eager mount，却开始追查 package skip marker 如何保留；
6. 第 16–18 步继续检查 skip hook 与 setup 顺序，直到上限，仍未编辑。

参考修复实际选择撤销那段依赖 eager import 的 package-level skip 行为。Agent 把“marker 行为保持懒惰”理解成“还必须保留旧 package skip 结果”，试图同时满足两个互相冲突的边界，于是陷入了证明循环。

## 长推理为什么突然爆发

第 15 个完整响应单次产生 6,722 个输出 Token，其中 6,587 个是推理 Token：

- 占本轮全部输出的 58.33%；
- 占本轮全部推理的 67.70%。

这次爆发发生在它已经找到修复点之后，内容主要是在调和 marker 生命周期和 lazy discovery，并不是 memory_context 太长造成的。模型可见记忆只有约 351 Token；真正导致累计输入上涨的是每个工具回合都重新携带不断增长的会话历史。好在 91.09% 输入命中了 provider 缓存，但请求次数和推理输出仍然真实消耗服务器资源。

## 题目是否太难

代码层面不是。零 API 预检已经证明：

- 基础提交能稳定触发隐藏失败；
- 参考提交通过隐藏验收；
- 参考环境的 3 个聚焦公开测试通过；
- Agent 自己也在预算内找到正确文件、正确函数和正确因果关系。

它难在决策边界，而不是定位或编码本身。继续单纯增加 Token 上限可能让它最终写出补丁，但也可能只是继续奖励历史调查，不能解决插件当前缺少“从 resolved contract 转入实现”的控制问题。

## 本轮还发现的两个测量问题

第一，运行记录中的 `memory_delivery_payload_tokens=285` 只反映后端索引，没有反映插件自动 explain 后最终渲染给模型的 contract。模型可见文本实测约 351 Token，provider 总输入仍然准确，但 MemoryOS 子项归因需要单独修正。

第二，绝对请求上限由外部轮询器执行。第 18 次 provider 调用已经发出后才被终止，因此只有 17 个完整 usage 记录。后续应把绝对上限移到 provider **发送前** 的同步 guard，既不浪费最后一次请求，也不会留下无法精确计费的半截响应。

## 下一步建议

先不要加预算，也不要设置“第六次必须写”这类固定步数。下一版应做三个通用修改：

1. action-ready 且工作区仍干净时，如果 Agent 转去 `git log/show/blame/reflog`、CHANGELOG 或上游补丁重建，触发一次状态型 drift guard，明确当前 checkout 与 resolved contract 已足够；只有代码直接矛盾或聚焦测试失败才允许重开调查。
2. 将绝对预算改为 provider 发送前检查。
3. 分别记录后端 index、自动 explain 和最终模型可见渲染文本的 Token。

这三项都可以按行为状态触发，不写 pytest、DeepSeek 或本题专用规则，也不依赖固定第几步。完成零 API 测试后，如要再次调用 provider，需要单独授权一次新的校准运行；本题仍只用于开发校准，之后证明插件效果必须换一题做 held-out A/B/C。

机器可读报告见 `report-live-r1.json`，冻结边界见 `acceptance-lock.json`，原始证据保存在 `D:/dsh-medium-success-calibration-v1/live-r1/outputs/msc_progressive`。
