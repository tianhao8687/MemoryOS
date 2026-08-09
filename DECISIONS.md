# Implementation decisions

This file records the non-blocking defaults selected while executing the V1.0, V2.0, and V2.1 task specifications.
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
7. Preserve the V1 fixed linear weights as the frozen baseline. V2 fuses FTS/vector/graph/temporal
   rankings with weighted RRF, then applies scope/freshness/evidence/feedback factors, optional top-N
   reranking, and MMR. Every selection keeps a machine-readable trace.
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
15. Do not crawl or index repository source. Git integration may read only an explicitly anchored
    file and persist a bounded excerpt plus symbol/path/hash metadata; it never stores a repository
    snapshot or scans the full tree.
16. Use an engineering-ledger visual system for the React UI: graphite navigation, cool gray canvas,
    teal verified state, amber conflict state, table/list-first layouts, and provenance typography.
17. Keep third-party client setup claims narrow. The MCP protocol is verified with a real Python
    stdio client and the packaged executable; Cursor and Claude Code snippets are clearly marked as
    configuration templates because their UIs were not automated in this workspace.
18. Keep V1 memories as the lifecycle/provenance envelope and add normalized Claims as the truth
    unit. Migration does not fabricate claims for old rows; normalization is conservative and lazy.
19. Resolve entity aliases only inside the same scope and entity type. Similarity can propose a
    merge, but persisted merge redirects require an auditable event.
20. Treat `valid_from/valid_to` as world validity and `recorded_at` as knowledge time. Current Truth
    accepts both bounds and never collapses them into one timestamp.
21. Use Tree-sitter language grammars for Python/TS/JS/Rust anchors. Unsupported languages use
    bounded path/snippet hashes; parser failure must not trigger whole-repository inspection.
22. Exclude stale claims from current context by default, downweight and label suspect claims, and
    make refresh produce a candidate instead of mutating accepted evidence.
23. Keep exact NumPy as the portable vector index. Offer `sqlite-vec` through an optional adapter;
    missing or broken extensions yield an unavailable capability and never prevent FTS startup.
24. Consolidation requires at least three independent sources across seven days by default. It
    persists proposals and counterevidence but cannot activate or delete memory.
25. User feedback affects retrieval utility only after validating that the memory belonged to the
    referenced RetrievalRun. It never changes Claim status or Current Truth directly.
26. Treat synthetic and hand-authored MemoryBench fixtures as pipeline evidence, not real-model
    accuracy. Without an actual paired coding-agent harness, record an external blocker and make no
    effectiveness claim.
27. Keep a stable ClaimIdentity per scoped subject/predicate and append ClaimVersion snapshots for
    every lifecycle/freshness transition. Current Truth reads version transaction intervals rather
    than reconstructing history from the mutable V2 projection.
28. Route obvious semantic relationships through deterministic rules. Only uncertain pairs may
    reach a bounded relationship judge; persist abstention/failure metadata and never mutate truth
    from an unconfirmed result.
29. Make sqlite-vec the persistent live semantic path when installed, isolated by
    provider/model/dimensions. Exact NumPy and FTS5 remain explicit, observable fallbacks.
30. Treat health temperature as retrieval governance, not truth. Archive is logical and reversible;
    refuse to archive the only non-archived accepted support for a ClaimIdentity.
31. Reject grounded-model consolidation output containing any support/counter ID outside the input
    whitelist or appearing on both sides. Fall back to an explicitly labeled offline extractive
    candidate, never an active fact.
32. Separate blind benchmark runtime payloads from scorer gold and warn on perfect scores. A real
    model mode is evidence only when an endpoint actually executed; absence is `external_blocker`.
33. Require V2.1 release evidence to be produced after merge from a clean `main` checkout, including
    packaged 0001→0003 migration and restart smoke.
