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
34. Treat all existing retrieval, confidence, feedback, and health coefficients as heuristic
    baselines unless a versioned calibration profile names its dataset, objective, code revision,
    and holdout result. Use pinned public Git history only for an explicitly silver retrieval set:
    runtime queries and qrels remain physically separate, query repositories are held out by split,
    source/License artifacts are hashed, and future/cross-scope guards remain non-tunable safety
    constraints. Silver labels cannot authorize truth-state mutation or health automation.
35. Do not solve silver-label coupling by adding more samples from the same label generator. Build
    human retrieval gold from runtime-only inputs: never load silver qrels during sampling, keep the
    existing test repository sealed, randomize candidate order independently for at least two blind
    reviewers, and require a separate adjudicator. Preserve Git history, real workloads, repository,
    and time strata as named slices; report source concentration, overlap, leave-one-source-out, and
    worst-slice results instead of approving weights from one pooled average. An overlapping real
    workload is diagnostic only, and pending or model-authored labels cannot be called human gold.
36. Treat multi-agent blind review as a provisional rubric stress test, not a substitute for human
    annotation. Invalidate an entire reviewer attempt after any isolation breach and replace it
    before adjudication; preserve the incident even when no control label was exposed. Freeze the
    adjudication policy before reading decisions, adjudicate every core disagreement, hash the
    complete chain, and open silver qrels only afterward for a clearly labeled proxy diagnostic.
    Report chance-adjusted agreement beside raw agreement, and prohibit model-only labels from
    fitting or approving production retrieval weights.
37. Make human annotation optional rather than a production dependency. The active AI-only route
    requires order-swapped pairwise votes from at least three genuinely distinct model families and
    treats their probabilistic consensus as downweighted weak supervision. Calibrate model-family
    reliability only against executable anchors. Learn only raw retrieval utility features with
    non-negative regularization and repository-level holdouts; archive, privacy, temporal, stale,
    and cross-scope exclusions remain hard gates. A causal label requires a protocol-valid,
    runtime-identical real-agent full/minus-memory pair with a discordant outcome. Promotion then
    requires sealed outcomes across at least 50 tasks, three repositories, ten sequences, and two
    unseen agent models, with positive success CI, no safety/worst-repository regression, complete
    costs, and bounded latency/cost. Passing creates an activation candidate only; it never changes
    frozen production weights automatically.
38. Never inspect sealed promotion/test observations inside candidate fitting or hyperparameter
    selection: fit on train and select regularization on repository-held-out development only.
    Project learned weights into a strict candidate-only shadow scorer and exercise that scorer
    through the actual RetrievalPipeline and ContextCompiler, with randomized frozen-baseline versus
    candidate agent runs. Bind each executable row to the learned-profile hash, shadow-profile hash,
    exact task prompt, full runtime digest, repository commit, and retrieval config. The production
    MemoryService continues to construct RetrievalPipeline without a profile; there is no implicit
    activation via settings, CLI, HTTP, or MCP.
39. Treat tasks, not repeated agent executions, as the independent promotion sample. Require a
    complete task-by-agent matrix, aggregate each task across agents/repeats before bootstrapping,
    and separately reject worst-agent regression. Verify the profile was actually executed by
    deriving the expected RetrievalRun config hash from its full payload; a declared profile SHA
    without the matching database run hash is invalid.
40. Keep deterministic fixtures out of learned production candidates. The frozen trainer requires
    both AI-jury weak supervision and real executable outcomes, rejects fixture observations before
    optimization, and records label-tier counts in the candidate hash. Promotion independently
    rejects any profile whose lineage contains a fixture label.
41. Count AI diversity by both canonical model family and provider. Require at least three of each,
    bind every vote to provider-reported model revision plus runtime/prompt/response hashes, reject
    one runtime claiming multiple identities, and cap each canonical family at one contribution.
    These are provenance controls, not cryptographic proof of provider honesty; executable anchors
    and sealed agent outcomes remain the correctness backstop.
42. Bind learned weights to their exact inputs, not only their protocol and aggregate counts.
    Reject duplicate observation IDs, require every mandatory evidence tier inside the train
    partition, canonicalize all train/dev observations independent of file order, and include that
    SHA-256 in the candidate profile hash. This makes stale, substituted, or accidentally repeated
    calibration inputs detectable before shadow evaluation.
43. Publish every protocol-valid real-agent ablation repeat, including unchanged outcomes, rather
    than selecting only favorable runs. Only a discordant functional outcome may become a causal
    TRAIN label; latency and token reductions remain descriptive until a separately frozen utility
    objective exists. An interrupted experiment may reuse a completed arm only after its derived
    manifest and full runtime digests match and its individual protocol gates pass. Published
    summaries exclude credentials and raw logs, and no such evidence changes production weights.
44. Treat public relevance training as an initialization prior, not a full retrieval scorer. A
    public profile may change only the relative FTS/vector RRF ratio in a non-production shadow;
    preserve their total scale and freeze graph, temporal, RRF K, MMR, downstream freshness/scope/
    feedback/truth factors, and all hard gates. Bind the shadow to the exact vector model revision,
    source hash, and feature adapter, verify that identity against the live embedding service, and
    require diagnostic executable ablations to emit no causal training observations. Production
    activation still requires independently held-out causal outcomes and an explicit promotion.
45. Reject a public FTS/vector prior for production unless its real-pipeline replay covers at least
    three repositories, has a positive repository-stratified paired-bootstrap lower bound for
    NDCG@10, does not reduce overall required Recall@5, and has no per-repository NDCG@10 or required
    Recall@5 regression. A passing public gate may advance only to causal shadow evaluation; it is
    never a production approval. The first BGE/SWE-Gym candidate (19.25% FTS / 80.75% vector)
    improved the 52-query point estimates but failed repository coverage, confidence-bound, and
    Pandas worst-repository gates, so the frozen 50/50 production ratio remains unchanged.
46. Separate query routing from numeric score calibration. Following the named-retriever and phased
    ranking patterns used by mature search systems, a query planner may select only an immutable,
    versioned recipe from an allowlist: exact, semantic, relational, temporal, complex, or the safe
    hybrid fallback. A recipe controls active channels, RRF fusion, the bounded reranker policy, and
    diversity policy; it cannot emit executable code, arbitrary weights, thresholds, or safety
    overrides. Candidate retrieval, fusion, governance, reranking, and diversity are separate
    stages. Exact-code Shadow execution uses persisted Source Anchors as a structured channel rather
    than scanning the repository or pretending lexical retrieval is symbol lookup. Router v2 uses
    discrete evidence signals and stable reason codes; the rule planner emits reason codes instead
    of uncalibrated numeric confidence, and no numeric threshold controls execution. Unclassified input fails
    closed to the safe recipe. Every channel reports requested, applicable, attempted, executed,
    contributing, and degraded state; actual reranker mode, fusion weights/K, score contract, and
    stage timings are persisted. Routed RRF is normalized to a bounded contract checked by Context
    Compiler, while production retains its legacy raw-RRF contract and frozen safe-hybrid recipe.
    Query-adaptive execution requires an explicit registry-hash-bound Shadow profile, is mutually
    exclusive with scoring/RRF shadows, and fails closed if the registry changes. The router is an
    architecture candidate, not evidence that its choices improve outcomes; 80/1000 pool bounds,
    40-item rerank window, RRF values, and MMR lambda remain named heuristic baselines. Numeric
    changes stay in the independent calibration track. Routing promotion aggregates independent
    tasks rather than repeated runs, binds the complete evaluation set, and requires sealed public
    real-agent evidence, a complete agent/repeat matrix, positive paired success CI, no safety or
    worst repository/agent/recipe regression, and bounded complete latency/cost. Passing produces
    only an activation-review candidate and never changes production automatically.
47. Optimize MemoryOS for minimum sufficient context under a task-success constraint, not for
    retrieval count, compression ratio, or any single context-density score. Functional success,
    provider usage, safety failures, attribution completeness, and worst-group behavior remain
    separate release dimensions.
48. Separate the production Context Response from diagnostic evidence. MSC production payloads
    contain only the text and minimal state needed by the current agent turn; query plans, sections,
    manifests, candidate features, retrieval traces, and legacy/MSC comparisons are persisted on the
    exact RetrievalRun and read on demand by retrieval_run_id.
49. Preserve ContextRequest.budget permanently as a legacy text-character budget. Token budgets use
    budget_tokens or a versioned BudgetProfile, carry tokenizer/counter identity, and constrain the
    complete MemoryOS production payload rather than silently changing an existing field's units.
50. Do not call an LLM in the core context-compilation path. Context atoms, rendering, budgeting,
    exact deduplication, and deltas are deterministic; any optional provider-backed memory operation
    is separately attributed and cannot be reported as zero when provider Usage is unavailable.
51. Treat ContextAtom as a compiled view, never as a source of truth. Memory, Claim, Source, immutable
    Git anchor evidence, bitemporal truth, and freshness remain authoritative; snapshots and atom
    manifests can be discarded and rebuilt.
52. Treat every selected relevant constraint and contested component as an indivisible bundle.
    Negation, thresholds, units, scope, exceptions, and both contested polarities cannot be truncated
    or partially delivered. A hard budget that cannot contain the bundle returns its minimum safe
    requirement instead of weakening the evidence.
53. Require an explicit previous_context_id for Delta Context and validate scope, expiry, policy,
    tokenizer, and integrity before diffing. Missing or invalid baselines, inefficient deltas, and
    unsafe structural changes fail closed to a labeled Full rebase without exposing prior-scope text.
54. Define ContextSnapshotRow as a disposable, scope-bounded cache. It is excluded from long-term
    backup and cleared on restore; an old cursor then reports snapshot_unavailable and safely rebuilds
    Full Context from primary evidence.
55. Activate only deterministic exact deduplication. Equivalent canonical claim identities may merge
    source references and evidence counts, while opposite polarity, contested sides, different valid
    intervals, freshness states, or constraint/observation roles never merge. Semantic deduplication
    remains behind the existing Shadow and evidence gates.
56. Fix the MCP tool set at process startup with deterministic all/core/governance/debug profiles.
    Dynamic discovery or deferred loading belongs to the client; a tool call cannot mutate the
    current connection's tools/list, and a smaller server schema is not itself proof of fewer
    provider input tokens.
57. Keep legacy as the default compiler until preregistered real coding-agent evidence simultaneously
    proves paired success noninferiority with adequate power, a significant reduction in actual
    provider input tokens, zero constraint/contested/cross-scope regressions, complete attribution,
    and no material worst-group regression. Dry runs, deterministic fixtures, estimated counters, or
    static text comparisons can validate plumbing only and cannot authorize activation.
