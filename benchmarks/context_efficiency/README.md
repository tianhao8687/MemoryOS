# V2.3 Context Efficiency executable benchmark

This benchmark creates a fresh Git workspace and Agent session for every task/condition, keeps the
same frozen base commit and scorer, and writes records for failures as well as successes. The five
preregistered MemoryOS conditions, the true `no_memory` baseline, and the optional
`msc_context_only` optimization arm are controller-owned: an Agent cannot select compiler, detail
level, response mode, delta base, or MCP profile.

Run the keyless end-to-end fixture from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\context_efficiency_bench.py `
  --manifest benchmarks\context_efficiency\manifest.json `
  --runtime runtime\context-efficiency-fixture.json `
  --conditions legacy_full msc_full msc_progressive msc_delta msc_delta_core `
  --cache-phases cold warm `
  --output build\context-efficiency\fixture
```

The fixture executes the complete model/tool loop, edits the isolated calculator repository, runs
pytest, exercises `memory_explain` and full-to-delta, and checks stable cold/warm request hashes. Its
summary is labeled `deterministic_fixture`; it is not provider performance, cost, or quality evidence.

## Local Qwen 8B

`runtime/qwen-local.json` is a portable Qwen3-8B template. This workstation's detected artifact is
Qwen3-VL-8B-Instruct Q4_K_M, exposed by llama.cpp as
`Qwen/Qwen3-VL-8B-Instruct-GGUF`; its exact revision and GGUF digest are frozen in
`runtime/qwen3-vl-local.json`.

For one local 8B model, use the capability-calibrated six-task/three-repository pilot rather than
the separate SWE-bench-derived stress workload. Start the pinned OpenAI-compatible endpoint on port
8080. For a true product before/after comparison, run the `no_memory` baseline against the
MemoryOS conditions with the neutral-prompt manifest:

```powershell
.\.venv\Scripts\python.exe scripts\context_efficiency_bench.py `
  --manifest benchmarks\context_efficiency\qwen_calibrated_v1\manifest-before-after.json `
  --runtime runtime\qwen3-vl-calibrated.json `
  --conditions no_memory legacy_full msc_full msc_progressive msc_delta msc_delta_core `
  --cache-phases cold `
  --tasks 6 `
  --output build\context-efficiency\qwen3-vl-before-after-v1
```

The older four-arm operability run remains available:

```powershell
.\.venv\Scripts\python.exe scripts\context_efficiency_bench.py `
  --manifest benchmarks\context_efficiency\qwen_calibrated_v1\manifest.json `
  --runtime runtime\qwen3-vl-calibrated.json `
  --conditions legacy_full msc_full msc_progressive msc_delta `
  --cache-phases cold `
  --tasks 6 `
  --output build\context-efficiency\qwen3-vl-calibrated-v1
```

To measure the tool-Schema optimization without changing the compiler or memory payload policy,
run a fresh three-arm comparison. `msc_full` and `msc_context_only` both use MSC FACT/full; the
only treatment difference is `all` versus the product's `context` MCP profile:

```powershell
.\.venv\Scripts\python.exe scripts\context_efficiency_bench.py `
  --manifest benchmarks\context_efficiency\qwen_calibrated_v1\manifest-before-after.json `
  --runtime runtime\qwen3-vl-calibrated.json `
  --conditions no_memory msc_full msc_context_only `
  --cache-phases cold `
  --tasks 6 `
  --output build\context-efficiency\qwen3-vl-token-optimization-v1
```

Ollama and LM Studio use the same adapter: change only `transport`, `base_url`, and the exact served
model identity. Provider usage wins. If a server omits usage, set `tokenizer.kind` to `huggingface`
and freeze an already-downloaded matching tokenizer with:

```powershell
.\.venv\Scripts\python.exe scripts\freeze_qwen_tokenizer.py C:\models\Qwen3-8B-tokenizer `
  --revision b968826d9c46dd6066d109eabc6255188de91218
```

Without provider usage or that exact tokenizer, the run records a protocol failure; it never invents
token counts. `scripts/build_real_workload_qwen_image.py` optionally builds and hash-locks a clean
Qwen Agent toolchain image. The model endpoint remains external and is never silently replaced.

## DeepSeek Harness

Install DeepSeek Harness `0.1.0-rc.5`, export `DEEPSEEK_API_KEY`, and run the same command with the
Harness runtime:

```powershell
$env:DEEPSEEK_API_KEY = '<secret>'
.\.venv\Scripts\python.exe scripts\context_efficiency_bench.py `
  --manifest <real-workload-manifest.json> `
  --runtime runtime\deepseek-harness.json `
  --cache-phases cold warm `
  --output build\context-efficiency\deepseek-pilot
```

The runner uses an isolated `DSH_HOME`, installs the bundled MemoryOS plugin into the headless
profile, binds cold/warm to one unique cache identity, runs fresh Harness sessions, and feeds the
resulting workspace patch to the existing hidden-test scorer. Missing Harness, key, endpoint, or
Docker infrastructure produces `external_blocker`; no alternate model runs.

For DeepSeek coding work, prefer `runtime/deepseek-harness-optimized.json` with the paired
conditions `no_memory` and `msc_context_only`. This `deepseek-optimized-offline-v3` preset removes
non-coding orchestration tools, exposes one argument-free 512-token MemoryOS context call, returns
only the context text, disables provider-backed session-title generation, and keeps tool network
access disabled. Once the model has enough evidence to state an edit, it must edit next and begin
editing within six repository-inspection calls. The phased controller stops read-only exploration
at 20 actual provider attempts or 800k cumulative input tokens. After a patch exists, 30 attempts
or 1.5M input tokens is the normal soft ceiling; each new patch fingerprint grants six more
attempts for focused verification or correction, up to the absolute 60-attempt / 3M-token
ceiling. Provider retries are written to a separate attempt ledger and consume the same ceilings.

For a matched-budget, single-instance A/B modeled on open-source coding-agent harnesses, use
`runtime/deepseek-harness-open-source-ab.json`. It records the phased budget in the runtime digest
and gives both arms the same `high` reasoning mode, 8,192-token per-request output cap, one retry
at most per failed provider call, and 1,200 seconds. Read-only work stops at 20 attempts; patched
work normally stops at 30, receives six-attempt progress extensions, and can never exceed 60
actual provider attempts or 3M cumulative successful-response input tokens. Provider-backed
session titles are disabled in both arms, so presentation-only LLM calls cannot bypass the
recorded Agent budget. Its task image
excludes installed copies of the target project and resolves Python `src/` layouts from the
checkout. The runner still captures and scores the working-tree patch at the ceiling.
The pinned image also supplies the profile directory as RC5's bare-module base and scopes Node's
internal ESM resolver flag to the trusted Harness launcher; both A/B arms use the same wrapper.

The first DeepSeek V4 Flash A/B task is deliberately medium-easy rather than long-horizon:
`requests-pr-6028` from the one-task
`benchmarks/real_workload/swebench_verified/requests_6028/manifest.json`. It is a real Requests
regression with a narrow URL-reconstruction edit and an offline focused hidden test. Do not use
the pytest MRO, cross-module serialization, or other high-difficulty pilot tasks for this first
Flash round. Pass `--budget-tokens 512` for this optimized Flash run; the run manifest records the
same effective budget used by the MemoryOS backend and Harness plugin.

Every invocation writes `run-manifest.json`, `records.jsonl`, `provider-usage.jsonl`,
`tool-events.jsonl`, `patches/`, `test-results/`, `summary.json`, and `summary.md`. Live pricing is
never fetched during reporting: costs use only the pricing snapshot embedded in the runtime.
