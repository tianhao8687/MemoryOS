# MemoryOS V2.1 Reality Intelligence Hardening

## 目标与边界

V2.1 解决 V2 在“现实随时间变化”时的四类风险：当前行覆盖历史、冲突模型越权、向量路径只存在于 benchmark、以及旧记忆无限堆积。实现仍是本地单用户产品，不增加云同步、全仓源码采集或自动接受模型结论。

## ClaimIdentity / ClaimVersion

`ClaimIdentity` 是稳定语义身份；每次状态、对象、有效期、freshness 或人工决策变化都会追加 `ClaimVersion`。旧版本只关闭 `transaction_to`，不原地改写内容。查询顺序固定为：

1. 用 `as_known_at` 选择 transaction interval 中可见的版本；
2. 用 `valid_at` 过滤现实有效期；
3. 在同一 subject/predicate/scope 内求 accepted、conflicting 与 resolution state；
4. 返回 version、reason、actor、transaction/valid intervals 和证据。

这允许回答“今天看来当时是什么”和“当时我们知道什么”两个不同问题。

## Semantic Conflict 2.0

规则层先处理可证明的 equivalent/supports/contradicts/independent。只有 `uncertain=true` 的 pair 具有 `model_eligible=true`。模型只看到两个 bounded claim 和必要 evidence；输出必须通过 schema。每次尝试写入 Possible Conflict：

- rule/model relationship 与 confidence；
- pending/confirmed/dismissed/abstained/error；
- provider/model fingerprint、prompt version、evidence hash；
- 人工 resolution actor、reason 与时间。

Abstain、timeout、非法 JSON 或 provider exception 都不能改变 accepted truth。

## Persistent ANN

Memory embedding 写入时同步写入 sqlite-vec 文件；namespace 由 provider、model 与 dimensions 唯一确定，避免不同向量空间混合。`ann_index_state` 记录路径、状态、item count、错误和 rebuild time。CLI/API/Settings 可查看和重建。

sqlite-vec 是实时首选路径；exact NumPy 是显式 fallback，FTS5 是离线保底。Doctor 会分别报告 provider、sqlite-vec runtime 与 namespace 状态。

## Grounded Consolidation 与 Memory Health

Abstractive consolidation 必须返回输入白名单中的 support/counter memory IDs，并跨独立来源；否则拒绝或降级为带 `abstraction_mode=offline-extractive-fallback` 的候选。provider fingerprint、prompt version、support/counter IDs 和 lineage 一起持久化。

Health score 只用于治理，不改变事实真值。温度为 Hot/Warm/Cold/Archived，每项都返回原因。Archive 是逻辑、可恢复状态，且不能归档某维度唯一 accepted current truth。Distillation 只接受 Cold/Archived IDs，输出始终是 candidate。

## 评测诚实性

CodingMemoryBench 在运行前拆分 input 与 gold，runtime adapter 明确拒绝 gold key。报告同时给出 baseline、V2 和 V2+model；无真实 model runner 时后者标记 `external_blocker`。满分会生成 warning，提醒扩展 adversarial case，而不是宣称泛化完成。

真实 coding-agent 协议要求至少 50 个 paired task。当前环境没有 `MEMORYOS_AGENT_BASE_URL`/`MEMORYOS_AGENT_MODEL` 与凭据，因此只保存 blocker；50-task deterministic fixture 仅验证配对、指标与 bootstrap CI plumbing，`effect_claim=none`。

## 证据入口

- 完整门禁：`scripts/verify_v21.py`
- A33–A52：`scripts/acceptance_v21.py`
- 盲测：`scripts/coding_memory_bench.py`
- 100k 全管线：`scripts/benchmark_v21_pipeline.py`
- paired agent：`scripts/agent_ab_v21.py`
- merged-main release：`scripts/main_release_smoke.py`

机器报告全部位于 `docs/verification/v2.1/`，字段和阈值映射见 `docs/ACCEPTANCE.md`。
