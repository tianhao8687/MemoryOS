# Changelog

## Unreleased

### Added

- V2.2 repository-level real-workload manifests with public/private/fixture tiers, license and source provenance, temporal Git validation, solution-leak rejection, and digest-pinned hidden tests.
- Three-condition `no_memory / flat_memory / memoryos` execution with sanitized base-only repositories, isolated MCP sidecars, real Retrieval 2.0 usage gates, bounded non-root agent containers, fresh scoring checkouts, networkless hidden tests, and canary leakage scans.
- Paired success/safety/cost/latency bootstrap reports with strict dry-run truthfulness and confirmatory diversity, accounting, image, prompt-parity, and egress gates.
- A deterministic container fixture and pinned MarkupSafe public-history smoke dataset for infrastructure validation only.
- Explicit task publication timestamps, memory-only cross-project repository validation, and a hard `real_coding_agent` evidence-type gate for confirmatory claims.

### Changed

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
