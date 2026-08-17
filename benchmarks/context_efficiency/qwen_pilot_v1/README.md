# Qwen3-VL 8B context-efficiency pilot v1

This public-replay pilot freezes six tasks across exactly three repositories:

- `pylint-dev/pylint`: `pylint-pr-4551`, `pylint-pr-6528`
- `pytest-dev/pytest`: `pytest-pr-5787`, `pytest-pr-6197`, `pytest-pr-10356`
- `mwaskom/seaborn`: `seaborn-pr-3069`

The tasks freeze two chronological, within-repository workload sequences while preserving a
fresh agent session and workspace for every task/condition run:

- `pylint-pilot-longitudinal`: `pylint-pr-4551` (step 1) then `pylint-pr-6528` (step 2)
- `pytest-pilot-longitudinal`: `pytest-pr-5787` (step 1) then `pytest-pr-6197` (step 2)

The seaborn task and `pytest-pr-10356` remain single-task sequences. The first three tasks and
memories come from `swebench_verified/cross_repo_v1`; the remaining three come from
`swebench_verified/label_seek_v1`. Hidden patches are byte-for-byte copies of those frozen
sources, and their SHA-256 values remain locked in `manifest.json`.

The detected local model is Qwen3-VL-8B-Instruct Q4_K_M, served by llama.cpp under the exact
model id `Qwen/Qwen3-VL-8B-Instruct-GGUF`. Run the 24-run, cold-cache pilot from the repository
root after starting the endpoint on `127.0.0.1:8080`:

```powershell
.\.venv\Scripts\python.exe scripts\context_efficiency_bench.py `
  --manifest benchmarks\context_efficiency\qwen_pilot_v1\manifest.json `
  --runtime runtime\qwen3-vl-local.json `
  --conditions legacy_full msc_full msc_progressive msc_delta `
  --cache-phases cold `
  --tasks 6 `
  --budget-tokens 6000 `
  --output build\context-efficiency\qwen3-vl-pilot-v1
```

This dataset and its output are pilot evidence only. They are not a formal product-effect claim.
