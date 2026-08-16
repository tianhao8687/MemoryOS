# Qwen 8B calibrated context-efficiency pilot v1

This pilot measures the MemoryOS execution path at a task size appropriate for one local 8B
model. It freezes six bounded one-function repairs across three isolated fixture repositories.
Every task still requires real tool use, a source patch, a visible test, and a Docker-isolated
hidden test; the fixture label describes provenance and does not make the agent deterministic.

The six tasks form three two-step, within-repository workload sequences. Every task/condition
still gets a fresh model session and workspace, as required by the experiment protocol.

For the product before/after comparison, use `manifest-before-after.json`. Its task prompts are
identical across arms and contain no instruction to call MemoryOS. The `no_memory` arm exposes only
workspace tools; the treatment arms additionally expose and enforce their frozen MemoryOS policy.
This makes the report a paired comparison of the same model without versus with MemoryOS.

```powershell
.\.venv\Scripts\python.exe scripts\context_efficiency_bench.py `
  --manifest benchmarks\context_efficiency\qwen_calibrated_v1\manifest-before-after.json `
  --runtime runtime\qwen3-vl-calibrated.json `
  --conditions no_memory legacy_full msc_full msc_progressive msc_delta msc_delta_core `
  --cache-phases cold `
  --tasks 6 `
  --budget-tokens 6000 `
  --output build\context-efficiency\qwen3-vl-before-after-v1
```

`summary.md` reports every treatment directly as `no_memory -> MemoryOS`, including paired success
transitions, provider tokens, requests, TTFT, latency, and treatment-integrity checks.

Run the 24 real-agent conditions after starting the pinned local llama.cpp endpoint:

```powershell
.\.venv\Scripts\python.exe scripts\context_efficiency_bench.py `
  --manifest benchmarks\context_efficiency\qwen_calibrated_v1\manifest.json `
  --runtime runtime\qwen3-vl-calibrated.json `
  --conditions legacy_full msc_full msc_progressive msc_delta `
  --cache-phases cold `
  --tasks 6 `
  --budget-tokens 6000 `
  --output build\context-efficiency\qwen3-vl-calibrated-v1
```

This is pilot evidence for end-to-end operability, not a formal product-effect claim. The
separate `qwen_pilot_v1` SWE-bench-derived dataset remains available as a stress workload and
must not be substituted for this calibrated acceptance run.
