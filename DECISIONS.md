# Implementation decisions

This file records the non-blocking defaults selected while executing the V1.0 task specification.
They are implementation commitments, not future proposals.

## Active decisions

1. Use UUIDv4 strings for all externally visible identifiers. The specification permits UUID or
   ULID; UUIDv4 keeps the dependency surface smaller and is natively available in Python.
2. Store all timestamps as UTC and serialize them as ISO 8601 at API boundaries. The React UI uses
   `Intl.DateTimeFormat` so display follows the browser's local timezone.
3. Use a normalized Git remote hash as stable repository identity when a remote exists. Without a
   remote, persist a marker under `.git` based on the repository root and first commit. A branch
   scope key is `<stable-repository-key>:<branch>`.
4. Treat every Agent/extractor write as a candidate. Only an explicit confirmation operation can
   promote it to active; immediate activation is restricted to explicitly manual writes.
5. Detect conflicts by scope plus normalized semantic key, falling back to subject/title where a
   key is unavailable. Never overwrite an active value silently; require `supersede`, `keep_both`,
   or `reject`.
6. Use SQLite WAL + FTS5 as the source of truth and offline search baseline. Optional embedding and
   OpenAI-compatible extraction adapters are disabled by default and may not make core behavior
   unavailable.
7. Rank retrieval using explicit weights: lexical 0.32, semantic 0.22, scope 0.18, importance 0.12,
   recency 0.08, confidence 0.08. In FTS-only mode, semantic contributes zero and the response mode
   states this truthfully.
8. Interpret the context `budget` as a character budget in V1. The output reports
   `characters_used`; this avoids pretending to have a tokenizer when providers are optional.
   Task-scoped memory without an explicit TTL receives a seven-day default; working memory in
   broader scopes remains allowed but is visibly warned about in the UI.
9. Use one local bearer token for HTTP writes. The bundled UI receives it as an HttpOnly,
   SameSite=Strict localhost cookie; external HTTP callers read the same token file. MCP stdio is
   authorized by the local process boundary and does not use the HTTP token.
10. Enforce loopback-only HTTP binding even when the host is reassigned at runtime. Read endpoints
    remain unauthenticated inside that boundary; V1 is not a multi-user or network service.
11. Implement forget as a logical state transition so normal retrieval stops returning the memory
    while minimal provenance and audit history remain explainable. Physical purge is outside V1.
12. Use versioned ZIP containers for SQLite backups and JSONL interchange. Validate exact archive
    members and SHA-256 before use; restore creates a safety backup and validates SQLite before
    replacing live data.
13. Publish Windows as a PyInstaller `onedir` distribution rather than a single-file executable.
    This makes bundled migrations/UI assets explicit and gives faster, more diagnosable startup.
14. Let `serve` select a free loopback port when `--port 0` (the default). Persist the chosen port in
    `runtime.json` and print it before starting Uvicorn.
15. Do not crawl or index repository source. Git integration records only repository identity,
    root, remote, branch, and HEAD; memory text must be explicitly supplied by a user or Agent.
16. Use an engineering-ledger visual system for the React UI: graphite navigation, cool gray canvas,
    teal verified state, amber conflict state, table/list-first layouts, and provenance typography.
17. Keep third-party client setup claims narrow. The MCP protocol is verified with a real Python
    stdio client and the packaged executable; Cursor and Claude Code snippets are clearly marked as
    configuration templates because their UIs were not automated in this workspace.
