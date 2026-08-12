# MemoryOS human review v1 pilot

This directory turns runtime-only calibration inputs into a blinded dual-review pack. It is designed
to break dependence on Git path-overlap silver labels without pretending that model-generated or
blank labels are human gold.

The initial pack samples 12 queries from each non-test query repository using deterministic time
strata. That gives 48 calibration cases across four train repositories and 12 validation cases from
the held-out dev repository. One public MemoryOS real-workload task is included as a separate
diagnostic slice. The existing lazygit test split is never loaded or sampled.

Build and validate from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\build_human_review_pack.py
.\.venv\Scripts\python.exe scripts\validate_human_review_pack.py
```

The generated `data/blind/reviewer-*.jsonl` files contain queries and candidate memories but no
silver relevance, eligibility, target commit, workload expectation, confidence, or importance.
`data/control/source_map.jsonl` is scorer-only and must not be given to reviewers. Copy the matching
blank file from `data/templates/` outside the repository before annotation; do not edit the pinned
template in place.

Validate completed independent reviews and inspect exact agreement:

```powershell
.\.venv\Scripts\python.exe scripts\validate_human_review_pack.py `
  --response build\human-review\reviewer-a.responses.jsonl `
  --response build\human-review\reviewer-b.responses.jsonl
```

Add `--adjudication build\human-review\adjudicated.jsonl` only after an independent adjudicator has
resolved every pair. The rubric and promotion rules are in [`RUBRIC.md`](RUBRIC.md).

## Completed model-only exercise

The checked-in [`model_review/`](model_review/) bundle records two effective blind model reviews
and a separate model adjudication of all 527 disagreements. It is fully hash-pinned and
reproducible, but remains `model_adjudicated_provisional`: raw relevance agreement is 75.70% while
chance-adjusted Cohen's kappa is only 0.203. It is useful for rubric debugging and active-learning
triage, not for fitting or approving production weights, and it does not change this pack's
`pilot_unlabeled` human-label status.

## What this optional pilot can and cannot establish

It can expose disagreement between path-overlap proxies and human downstream utility, measure
repository/source slices separately, and prepare leave-one-source-out evaluation. It cannot approve
production weights: labels and adjudication are pending, the only real-workload task shares the
MarkupSafe repository with the Git source set, and there is no external human-gold test. Those
limitations are machine-readable in `data/coupling_audit.json` and `data/manifest.json`.

MemoryOS now uses the separate
[`../ai_calibration_v1/`](../ai_calibration_v1/) AI-only executable route for production-weight
calibration. Human completion of this pack remains useful if human gold is desired, but it is not a
release or promotion prerequisite. The AI-only route still cannot reuse this model exercise as
truth: it requires three distinct model families, order-swapped pairwise votes, real full/minus
ablations, repository holdouts, and sealed multi-agent executable outcomes.
