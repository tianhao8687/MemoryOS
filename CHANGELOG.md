# Changelog

## Unreleased

### Added

- A pinned public Git silver retrieval-calibration dataset with 300 repository-held-out queries,
  3,656 commit-memory candidates, 9,600 graded judgments, future/cross-scope guards, separate
  runtime/qrels artifacts, license hashes, and deterministic offline rebuilds.
- Strict calibration schemas, public-source materialization, first-parent temporal mining, artifact
  integrity/reference validation, and build/validate CLIs with deterministic fixture tests.
- A 61-case blind human-review pilot with 1,922 candidate judgments per reviewer, deterministic
  repository/time sampling, sealed-test enforcement, independently shuffled dual assignments,
  blank response templates, scorer-only source mapping, adjudication validation, and artifact hashes.
- A machine-readable coupling audit that exposes source concentration and repository overlap,
  preserves named source slices, and prepares leave-one-source-out evaluation without claiming that
  pending labels are gold.
- A hash-pinned model-only blind-review exercise with two effective 1,922-row reviews, complete
  third-party resolution of 527 disagreements, a reproducible final provisional artifact,
  chance-adjusted agreement, protocol-incident auditing, and post-adjudication silver diagnostics.
- A frozen AI-only executable calibration protocol with three-family order-swapped pairwise jury
  aggregation, entropy-weighted weak supervision, raw retrieval features, non-learned safety gates,
  non-negative regularized weight fitting, repository holdouts, and an explicit no-auto-activation
  promotion decision.
- A randomized full/minus-memory real-workload runner, exact prompt/runtime pairing, causal-label
  extraction from discordant executable outcomes, sealed multi-agent promotion checks, and a
  SHA-256-pinned readiness inventory that fails closed while real evidence is incomplete.
- An explicit candidate-only scoring projection and randomized baseline/candidate real-agent shadow
  runner. The training CLI rejects sealed test inputs, profile/evaluation/runtime hashes are bound
  end-to-end, and the normal production service keeps the byte-compatible frozen baseline config.
- The frozen trainer rejects deterministic-fixture labels and requires both multi-family AI weak
  supervision and real executable label tiers; promotion rejects any fixture-trained profile.
- AI-jury inputs now require three providers as well as three canonical model families and bind
  provider/model revision/runtime/prompt/response identities; runtime relabeling fails closed.
- Candidate profiles now bind the canonical SHA-256 of every train/dev observation; duplicate IDs
  fail closed, and all required evidence tiers must occur in the training partition itself.
- A pinned SWE-bench Verified Requests proxy-auth task, dependency-free semantic hidden scorer,
  strict full/minus arm resume checks, and two real `gpt-5.6-sol` ablation repeats. One repeat
  produced a discordant executable TRAIN label; both selected-memory runs were faster, while the
  underpowered one-repository dataset was correctly rejected for fitting and production stayed
  frozen.
- V2.2 repository-level real-workload manifests with public/private/fixture tiers, license and source provenance, temporal Git validation, solution-leak rejection, and digest-pinned hidden tests.
- Three-condition `no_memory / flat_memory / memoryos` execution with sanitized base-only repositories, isolated MCP sidecars, real Retrieval 2.0 usage gates, bounded non-root agent containers, fresh scoring checkouts, networkless hidden tests, and canary leakage scans.
- Paired success/safety/cost/latency bootstrap reports with strict dry-run truthfulness and confirmatory diversity, accounting, image, prompt-parity, and egress gates.
- A deterministic container fixture and pinned MarkupSafe public-history smoke dataset for infrastructure validation only.
- Explicit task publication timestamps, memory-only cross-project repository validation, and a hard `real_coding_agent` evidence-type gate for confirmatory claims.
- A declarative retrieval-plan candidate architecture with immutable allowlisted recipes, a
  deterministic query router, channel-specific retrieval, RRF fusion, optional bounded
  cross-encoder reranking, recipe-controlled MMR, safe fallback, and persisted execution traces.
  Routing remains explicit diagnostic Shadow only; the production service executes the unchanged
  all-channel frozen baseline and the router cannot emit free-form weights.
- Retrieval execution is now split into candidate, fusion, governance, rerank, and diversity
  stages. Exact-code Shadow routes use persisted Source Anchors as a separate structured channel;
  every route records requested versus attempted/executed/contributing/degraded capabilities,
  actual reranker mode, fusion inputs, and per-stage latency.
- Router v2 removes query-time confidence thresholds and invented route-confidence constants.
  The rule planner emits stable intent-reason codes instead of uncalibrated pseudo-probabilities;
  unclassified queries fail closed to the frozen safe recipe. Routed RRF uses a bounded normalized
  fusion contract, which Context Compiler validates.
- A hash-bound routing promotion analyzer aggregates at task level and requires sealed public
  real-agent evidence, a complete task/agent/repeat matrix, positive paired success confidence,
  non-regression in worst repository/agent/recipe slices, no task safety regression, and bounded
  complete latency/cost evidence. Passing never activates production automatically.

### Changed

- Fixed scoring constants are now explicitly treated as heuristic baselines; the silver dataset is
  retrieval evidence only and does not authorize automatic truth or health decisions.
- Human-review data cannot be called human gold until two independent human reviews and independent
  human adjudication cover every pair. Human gold is optional; the active no-human promotion route
  instead requires diverse AI weak supervision plus external sealed executable evidence.
- Model-only adjudication remains a rubric and active-learning diagnostic even when complete; it
  cannot by itself approve production weights or satisfy the new multi-family pairwise jury gate.
- Historical search/context no longer mutates current expiration state, and recency ranking is evaluated relative to the requested valid-time snapshot.
- Agent patches are captured from the pinned base even after agent commits; post-agent host Git rejects control-plane tampering and runs with hooks and external configuration disabled.
- Agent adapters receive only a pre-created structured-result file, while bounded stdout/stderr remain outside the agent mount; POSIX writable binds enforce non-root UID compatibility.

## 2.1.0 - 2026-08-10

### Added

- Immutable `ClaimIdentity`/`ClaimVersion` transaction history and true valid-time/known-at reconstruction.
- Deterministic uncertainty router plus auditable Possible Conflict queue with bounded model eligibility, abstention safety, provider fingerprint, prompt version, and evidence hash.
- Persistent sqlite-vec namespaces by provider/model/dimension, exact fallback, doctor/status/rebuild operations, and packaged sqlite-vec runtime.
- Explainable Hot/Warm/Cold/Archived memory health, reversible archive, sole-current-truth protection, and candidate-only distillation.
- Grounded abstractive consolidation with validated support/counter IDs and independent-source constraints; explicitly labeled offline extractive fallback.
- Blind CodingMemoryBench hard negatives with runtime/gold isolation, baseline/V2/V2+model modes, perfect-score warnings, and 100k full retrieval/context latency evidence.
- Current Truth transaction details, Possible Conflicts review, Memory Health controls, and vector diagnostics in the responsive React Workbench.
- Explicit `0003_reality_intelligence_hardening` migration and backup format 3.

### Verification

- Added A33–A52 tests, evidence manifest, merged-main release smoke, and the `scripts/verify_v21.py` fail-fast entrypoint.
- The 50-pair real coding-agent run remains an explicit external blocker because no model endpoint or credentials were supplied. Fixture results are harness-only and `effect_claim=none`.

## 2.0.0 - 2026-08-10

### Added

- Claim/entity graph, evidence spans, semantic relations, scoped aliases, bitemporal truth, and `resolved/contested/stale/unknown` Current Truth.
- Tree-sitter Source Anchors for Python/TypeScript/JavaScript/Rust with Git fresh/moved/suspect/stale state, lazy caching, and replacement-candidate refresh.
- Retrieval 2.0 query planner, FTS/vector/graph/temporal union, RRF, MMR, per-result traces, exact NumPy `VectorIndex`, and optional `sqlite-vec` ANN.
- Task-aware context compiler with coverage, utility/cost budgeting, stale policy, contested-side inclusion, and persisted manifests.
- Consolidation candidates with counterevidence and lineage, auditable helpful/unhelpful feedback, and six schema-validated provider interfaces.
- Five V2 MCP tools, V2 HTTP/CLI operations, and six Memory Intelligence Workbench pages while preserving all V1 interfaces.
- MemoryBench V2 with frozen sample counts, baseline/V2 comparisons, 100k measured FTS5 P95, 30-task fixture A/B harness, bootstrap 95% CIs, JSON/HTML reports, and explicit real-model evidence labeling.
- `0002_memory_intelligence`, V1 backup import, V2 backup versioning, and packaged V1 DB → V2 migration smoke.

### Verification

- Added A15–A32 tests and evidence manifest while retaining A01–A14 regression gates.
- Real coding-agent A/B was not run because no real-model harness endpoint was configured. Fixture results are labeled harness-only and no real-model effect claim is made.

## 1.0.0 - 2026-08-09

### Added

- Python 3.12 + React 19 monorepo with a single fail-fast verification entrypoint.
- SQLite WAL/FTS5 storage, Alembic migration, UUID domain model, provenance links, relations,
  embeddings, settings, and audit events.
- Five scopes, six memory types, candidate-first lifecycle, TTL/validity expiration, logical
  forgetting, conflict detection, explicit resolution, supersession history, and explain output.
- FTS5 and optional hybrid retrieval with scope, importance, recency, and confidence ranking.
- Budgeted context builder with scope isolation, sectioning, deduplication, historical opt-in, and
  provenance references.
- Git repository discovery, stable remote-based identity, automatic branch scope key, and the
  source-code-hoarding guard.
- Seven stdio MCP tools, Typer CLI, FastAPI application, and bundled eight-page React management UI.
- Offline heuristic extraction and optional schema-validated OpenAI-compatible extraction and
  embedding providers.
- Secret redaction, loopback-only serving, bearer/cookie write authentication, Origin checks,
  rotating local logs, doctor diagnostics, versioned backup/restore, and JSONL interchange.
- Windows PyInstaller onedir build, backend wheel, clean-path production smoke, real MCP/HTTP
  cross-client tests, Playwright desktop/mobile flows, visual evidence, and a 10,000-record FTS5
  benchmark.

### Verification

- Added A01-A14 evidence mapping in `docs/ACCEPTANCE.md`.
- Added `docs/verification/verify-summary.json`, `performance.json`, `package-smoke.json`, desktop
  and mobile UI screenshots.
- The production smoke copies the release to a clean path containing spaces, validates the UI and
  all seven MCP tools, writes and confirms a memory, restarts the process, reads the same memory by
  HTTP, and runs packaged CLI status.
