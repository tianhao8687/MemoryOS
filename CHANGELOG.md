# Changelog

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
