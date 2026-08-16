# DeepSeek V4 Flash medium-success calibration v1

This is one success-first calibration run, not an A/B/C effect estimate. It
uses only the optimized `msc_progressive` condition and MemoryOS plugin
`0.1.10` on the real SWE-bench Verified task `pytest-pr-6197`.

The task, base commit, hidden scorer, runtime, plugin, prompt, and ceilings are
frozen before the first provider request. The agent runs in an isolated Linux
workspace with tool-network access disabled. The controller may prepare the
checkout and frozen runtime image, but hidden scoring remains outside the
agent-visible root.

Budget policy:

- zero provider retries and zero automatic repetitions;
- at most 8,192 output tokens per response;
- before a patch: 18 provider attempts, 400,000 cumulative input tokens, or
  32,000 cumulative output tokens;
- after a patch appears: absolute ceilings of 24 attempts, 600,000 cumulative
  input tokens, and 48,000 cumulative output tokens.

Success requires a non-empty patch, focused/public tests passing, the isolated
hidden scorer passing, no agent-side network access, and complete provider/tool
accounting. Because the plugin was tuned before and during calibration-task
selection, this task is excluded from later claims that the plugin improves
success rate. A later held-out task must be used for A/B/C comparison.

The controller command is:

```powershell
.\.venv\Scripts\python.exe scripts\context_efficiency_bench.py `
  --manifest benchmarks\context_efficiency\qwen_pilot_v1\manifest.json `
  --runtime benchmarks\context_efficiency\medium_success_calibration_v1\runtime-live.json `
  --conditions msc_progressive `
  --cache-phases cold `
  --task-id pytest-pr-6197 `
  --budget-tokens 1200 `
  --run-id medium-success-calibration-v1-live-r1 `
  --hidden-root benchmarks\context_efficiency\qwen_pilot_v1\hidden `
  --output <isolated-output-root>
```
