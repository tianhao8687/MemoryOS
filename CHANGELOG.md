# Changelog

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
