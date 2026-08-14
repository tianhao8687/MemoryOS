# MemoryOS V2.2 performance tiers

Performance evidence is split by the capabilities that actually executed. No tier is called a “full pipeline,” and none of these synthetic performance fixtures is evidence of coding-agent effectiveness.

| Tier | Dataset | Actually executed | Execution policy | Result |
| --- | ---: | --- | --- | --- |
| 1 — 100K FTS-first Core Pipeline | 100,000 memories | FTS5, `RetrievalPipeline`, `TaskAwareContextCompiler` | release/manual | search P50/P95 17.715/20.598 ms; context 239.949/263.738 ms |
| 2 — Hybrid Local Pipeline | 10K and 20K memories with matching real BGE embeddings/claims plus 100/200 relations | FTS, vector, Claim/Relation, temporal, context; sqlite-vec ANN and forced exact fallback | manual/scheduled only; not claimed as CI | 10K ANN/exact search P50/P95 278.454/313.424 and 1679.675/1733.581 ms; 20K 322.170/375.527 and 3054.057/3165.333 ms |
| 3 — Model-enhanced Pipeline | provider-dependent | embedding + model reranker, with model-provider fallback treated as failure | explicit manual provider run | not executed: no compatible reranker/relationship endpoint was configured |

Machine-readable evidence is indexed in [`performance-tiers.json`](verification/v2.2/performance-tiers.json). Tier 1 and Tier 2 reports record platform, provider/fixture identity, record counts, vector backend, reranker state, requested/executed/contributing/degraded channels, fallback state, and P50/P95.

Tier 2 contains zero `ClaimVersion` history rows: its temporal contribution is driven by `Claim` valid intervals. Bitemporal version-history scaling is measured separately by the Current Truth benchmark below.

Current Truth has a separate before/after SQL-scaling benchmark. On the same 1/10/1000-identity corpus and machine, the 1000-identity path changed from 7,046 SQL statements at P50/P95 1217.033/1311.148 ms to a constant 9 statements at 76.094/82.460 ms. The full baseline and hardened measurements are in [`current-truth-performance.json`](verification/v2.2/current-truth-performance.json); this is deterministic synthetic performance evidence, not an Agent-effect claim.

Run Tier 1:

```powershell
python scripts/benchmark_v21_pipeline.py --records 100000 --rounds 25
```

Run Tier 2 with a local FastEmbed installation and model cache:

```powershell
python scripts/benchmark_hybrid_pipeline.py `
  --records 10000 `
  --rounds 7 `
  --fastembed-path D:\MemoryOS-Lab\python\fastembed-0.8.0 `
  --model-cache D:\MemoryOS-Lab\models\fastembed
```

Repeat with `--records 20000 --rounds 3` for the measured 20K scale. The two raw reports are [`hybrid-local-10k.json`](verification/v2.2/hybrid-local-10k.json) and [`hybrid-local-20k.json`](verification/v2.2/hybrid-local-20k.json).

Tier 3 deliberately requires explicit provider arguments; `python scripts/benchmark_model_enhanced_pipeline.py --help` lists them. The runner exits non-zero if vector/reranker channels fall back, and never writes API keys into its report.

Reproduce the Current Truth comparison by checking out the baseline commit and current branch separately, then running the same command in each checkout. Pass the baseline JSON to the current run with `--baseline-report`:

```powershell
python scripts/benchmark_current_truth.py --rounds 7
```
