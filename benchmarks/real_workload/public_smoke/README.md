# Public replay smoke fixture

This fixture replays Pallets MarkupSafe PR 497 from the first-parent base commit at a cutoff
between the public pull request opening and its merge commit. Repository, license, pull request,
base, solution, timestamps, memory evidence, container image, and hidden overlay are pinned.
The recorded pull-request publication time is machine-checked to be no later than the replay
cutoff.

The bundled `fixture_agent` is deliberately deterministic. It exists only to exercise clone,
temporal validation, three isolated conditions, MCP sidecar transport, patch capture, hidden-test
scoring, leakage scans, and report generation. A report produced with that adapter must remain
`mode=dry_run` and `effect_claim=none`; it is not coding-model evidence.

From the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\build_real_workload_fixture_image.py
.\.venv\Scripts\python.exe scripts\real_workload_bench.py `
  --manifest benchmarks\real_workload\public_smoke\manifest.json `
  --runtime build\real-workload\fixture-runtime.json `
  --hidden-root benchmarks\real_workload\public_smoke\hidden `
  --mode dry_run `
  --tasks 1 `
  --run-id markupsafe-public-smoke
```

The upstream repository is BSD-3-Clause licensed. This fixture stores only a small original hidden
test overlay; it does not redistribute the upstream source tree or solution patch.
