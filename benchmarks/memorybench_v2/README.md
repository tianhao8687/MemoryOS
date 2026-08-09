# MemoryBench V2

MemoryBench V2 is the frozen quality benchmark for MemoryOS V2. It reports V1 baseline and V2 results side by side and never labels fixtures as real-model evidence.

Run the complete protocol:

```powershell
.venv\Scripts\python.exe scripts\memorybench_v2.py
```

The run records the seed, Git commit, dirty state, configuration hash, provider/model identity, environment, data provenance, and release-gate results. JSON and HTML reports are written to `docs/verification/v2/`.

Suites:

- E: 100 hand-authored extraction conversations.
- R: 250 deterministic retrieval queries.
- C: 200 semantic claim pairs.
- T: 120 bitemporal scenarios.
- G: 120 Git mutation state-machine cases; real Git integration is covered by the test suite.
- L: 80 consolidation sequences with repeated evidence and counterevidence.
- X: 150 scoped context-selection tasks.
- A: 30 paired fixture tasks for harness validation only.
- P: measured 100,000-record FTS5 P95 with the reranker disabled.

The real coding-agent A/B section remains `external_blocker` until an actual OpenAI-compatible coding-agent harness is configured. The fixture section validates pairing, metrics, and bootstrap confidence intervals; it is not an effectiveness claim.
