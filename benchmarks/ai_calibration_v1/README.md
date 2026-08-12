# AI-only executable calibration v1

This directory defines MemoryOS's no-human retrieval-weight calibration route. It does not treat
one model's confidence, majority vote, Git-derived silver labels, or deterministic fixtures as
ground truth. Production weights stay frozen until an explicit sealed promotion decision passes,
and even an approved decision never activates a profile automatically.

## Evidence ladder

1. Three or more genuinely distinct model families from at least three providers judge candidate
   pairs in both presentation orders. Each family contributes at most one family-level vote.
   Provider, model revision, runtime, prompt, and response identities are hash-bound; one runtime
   cannot rename itself into another family. Disagreement, order sensitivity, and entropy reduce
   the observation weight; invalid comparisons receive zero weight.
2. Selected memories are removed one at a time and the same real coding task is rerun. Only a
   protocol-valid discordant full/minus outcome creates an executable label. Deterministic fixture
   results test plumbing only.
3. A sign-constrained, regularized pairwise model learns candidate weights from raw retrieval
   features. Training uses train only and selects regularization on development; the command
   rejects sealed test, promotion, and deterministic-fixture observations. The frozen protocol
   requires both AI-jury weak supervision and at least one real executable label tier in the
   training partition. Observation IDs must be unique, and the candidate profile binds the
   canonical SHA-256 of every train/dev observation used to produce it.
   Archive, privacy, time, staleness, and scope exclusions remain hard gates outside the optimizer.
   Train and development repositories are disjoint.
4. A candidate is evaluated on at least 50 sealed tasks, three repositories, ten task sequences,
   and two agent models absent from training. Every task must run on every promotion agent model;
   bootstrap units are task-level aggregates, so repeated agents do not create fake sample size.
   Promotion requires a positive lower bound for success, no safety increase, no worst-repository
   or worst-agent regression, bounded latency and cost, and complete paired cost accounting.
5. Passing produces `approved_for_atomic_activation`, not activation. Applying a profile remains a
   separate explicit operation with rollback; v1 deliberately provides no automatic activation
   path.

## Checked-in state

[`protocol.json`](protocol.json) is the frozen machine-readable protocol.
[`readiness.json`](readiness.json) is a SHA-256-pinned evidence inventory. At this snapshot the
protocol and tooling are ready, and nine protocol-valid real-agent full/minus pairs now cover six
distinct SWE-bench Verified tasks across Requests, Pylint, pytest, and Seaborn. The original
Requests pilot contains the only discordant pair and therefore the only real executable TRAIN
label. Three cross-repository pairs and four later label-seeking pairs all retain unchanged
outcomes. The later batch includes order-balanced pytest repeats and fail-closed scorer audits; it
was adaptively enriched for training-data acquisition and is not a product-effect sample. This
mixed evidence prevents success-only selection but remains insufficient for fitting: the prior
blind review used one model family/provider, the frozen training gate still lacks usable labels
across three train repositories and a held-out development observation, and there is no candidate
or promotion approval. This status is intentionally `protocol_ready_evidence_pending`.

The cross-repository task pack is locked under
[`../real_workload/swebench_verified/cross_repo_v1`](../real_workload/swebench_verified/cross_repo_v1).
Its public-dataset revision, repository partitions, temporal provenance, scorer hashes, and
base/fix verification are pinned before the published outcomes. The later label-seeking packs are
locked under `label_seek_v1` and `label_seek_v2`, including run order and post-run audit hashes.
The evidence summaries record and exclude every scorer-invalid pair, agent-protocol violation, and
incomplete infrastructure attempt; none contributes to the nine-pair readiness count.

Evidence file hashes use Git-canonical text bytes: validators normalize CRLF checkouts to LF before
hashing, while still parsing and validating the referenced JSON semantics. This keeps the same
inventory valid on Windows and POSIX clones without weakening content binding.

Validate the inventory:

```powershell
.\.venv\Scripts\python.exe scripts\validate_ai_calibration.py
```

Run the machine stages after suitable independent inputs exist:

```powershell
.\.venv\Scripts\python.exe scripts\run_executable_ablation.py `
  --manifest benchmarks\real_workload\public_smoke\real_agent_manifest.json `
  --runtime build\real-workload\codex-runtime.json `
  --hidden-root benchmarks\real_workload\public_smoke\hidden `
  --task-id markupsafe-pr-497 `
  --memory-id warning-category-decision `
  --partition train `
  --repeat-id markupsafe-001 `
  --run-id markupsafe-warning-ablation-001

.\.venv\Scripts\python.exe scripts\ai_calibration.py jury `
  --votes build\ai-calibration\jury-votes.jsonl `
  --results build\ai-calibration\jury-results.jsonl `
  --utilities build\ai-calibration\utilities.jsonl `
  --features build\ai-calibration\candidate-features.jsonl `
  --observations build\ai-calibration\jury-observations.jsonl

.\.venv\Scripts\python.exe scripts\ai_calibration.py ablation `
  --runs build\ai-calibration\ablation-runs.jsonl `
  --output build\ai-calibration\ablation-report.json

.\.venv\Scripts\python.exe scripts\ai_calibration.py train `
  --observations build\ai-calibration\observations.jsonl `
  --output build\ai-calibration\candidate-profile.json `
  --shadow-profile build\ai-calibration\shadow-retrieval-profile.json

.\.venv\Scripts\python.exe scripts\run_weight_shadow.py `
  --manifest benchmarks\real_workload\public_smoke\real_agent_manifest.json `
  --runtime build\real-workload\codex-runtime.json `
  --hidden-root benchmarks\real_workload\public_smoke\hidden `
  --profile build\ai-calibration\candidate-profile.json `
  --task-id markupsafe-pr-497 `
  --repeat-id markupsafe-shadow-001 `
  --run-id markupsafe-weight-shadow-001

.\.venv\Scripts\python.exe scripts\ai_calibration.py promote `
  --profile build\ai-calibration\candidate-profile.json `
  --evaluations build\ai-calibration\sealed-evaluations.jsonl `
  --output build\ai-calibration\promotion-decision.json
```

All JSON and JSONL inputs use strict schemas; unknown fields, incomplete swap pairs, repository
leakage, fixture-only promotion evidence, reused training agent models, missing costs, and malformed
pairing dimensions fail closed. The ablation runner randomizes arm order per repeat, gives each arm
a fresh workspace, and requires matching task, commit, prompt, agent identity, evidence type, and
runtime fingerprint before pairing. Its single-condition source reports are expected to have no
three-arm effect claim; validity is assessed at the individual full/minus record level. Candidate
traces contain ranks and state/count features but no memory text, and a discordant valid pair emits
`training-observations.jsonl` automatically. Repository partitions still have to be assigned before
execution so a task cannot migrate between train and development after its outcome is seen.

The trained profile is not a production configuration. `run_weight_shadow.py` projects it into the
strict `candidate_shadow` schema, randomizes frozen-baseline/candidate order, and launches both arms
through the real MemoryOS retrieval and context path. The pair must keep the task prompt and full
agent-runtime digest identical, while retrieval config hashes must differ and the candidate record
must bind the exact shadow-profile digest. Only public tasks from repositories absent from train and
development can be marked sealed. The production `MemoryService` constructor still passes no
profile, and no settings, CLI, HTTP, or MCP production path loads one implicitly.

## Public relevance bootstrap

Public Git/SWE-Gym relevance data may initialize only the relative FTS/vector retrieval ratio. The
bootstrap trainer keeps repository-level train/dev/test partitions, uses repository-macro metrics,
caps per-query preference pairs, records leave-one-repository-out ranges, and emits a strict
`public_bootstrap_prior` with `production_eligible=false`. It cannot calibrate graph, temporal,
freshness, scope, truth, feedback, confidence, importance, reranking, or safety behavior.

`build_public_rrf_shadow.py` converts that prior into a narrower `rrf_channel_candidate_shadow`.
The converter preserves the frozen FTS+vector total scale, graph and temporal weights, RRF K, MMR,
and every downstream score factor and hard gate. The shadow binds the dataset, feature rows,
FastEmbed model revision/source, feature adapter, and converter hashes. MemoryOS refuses to run it
without the matching embedding model, and the benchmark MCP verifies the live embedding service
identity before indexing or retrieval.

The local bridge uses the exact FastEmbed `query_embed` and `passage_embed` methods used to create
the public training features while exposing the existing OpenAI-compatible embeddings interface.
Public-shadow executable ablations must pass `--diagnostic-only`; they produce no calibration
observation and cannot activate production weights. A typical local sequence is:

```powershell
.\.venv\Scripts\python.exe scripts\build_public_rrf_shadow.py `
  --profile D:\MemoryOS-Lab\training\public-bootstrap-v1\FINAL-swegym-bge-repo-macro.json `
  --output D:\MemoryOS-Lab\training\public-bootstrap-v1\SHADOW-swegym-bge-rrf.json

.\.venv\Scripts\python.exe scripts\serve_fastembed_openai.py `
  --dependency-path D:\MemoryOS-Lab\python\fastembed-0.8.0 `
  --model-cache D:\MemoryOS-Lab\models\fastembed `
  --model BAAI/bge-small-en-v1.5 `
  --vector-channel-id fastembed:BAAI/bge-small-en-v1.5@<revision> `
  --vector-channel-source-sha256 <source-sha256> `
  --vector-feature-adapter-sha256 <adapter-sha256>
```

The real-agent ablation command then receives `--rrf-channel-profile`,
`--embedding-base-url`, `--embedding-model`, and `--diagnostic-only`. Production remains on the
frozen baseline unless a separate causal dataset, sealed promotion run, and explicit activation all
pass.

## Query-adaptive retrieval routing shadow

Numeric calibration and query routing are separate experiments. The routing candidate follows the
same broad decomposition used by mature multi-stage search systems: plan, retrieve from selected
channels, fuse, optionally rerank a bounded window, then diversify. It does not ask a model to
invent a query-time weight vector. The deterministic router may select only a registry entry:

| Route | Approved recipe | Channels | Reranker | Diversity |
| --- | --- | --- | --- | --- |
| Exact code/symbol | `exact-symbol-v1` | FTS, vector, Source Anchor | Disabled | Disabled |
| Semantic | `semantic-hybrid-v1` | FTS, vector | If available | MMR |
| Relational/provenance | `relational-graph-v1` | FTS, vector, graph | If available | MMR |
| Historical/as-of | `temporal-as-of-v1` | FTS, vector, temporal | If available | Disabled |
| Multi-clause | `complex-hybrid-v1` | All | If available | MMR |
| Unclassified fallback | `safe-hybrid-v1` | Frozen production channels | If available | MMR |

Every recipe is immutable and hashable. The Shadow profile binds the complete approved registry;
unknown recipes, changed registry contents, and composition with a scoring or RRF-weight Shadow all
fail closed. Every RetrievalRun stores both the advisory route and the executed recipe. The normal
service deliberately records the recommendation but executes `safe-hybrid-v1`, preserving current
production behavior.

Router v2 uses explicit exact/relational/temporal/clause signals and stable reason codes. The
rule planner emits an intent-reason code instead of an uncalibrated numeric confidence, so no
pseudo-probability is thresholded to choose executable behavior. An unclassified query fails closed to `safe-hybrid-v1`. Exact lookup
queries the already-persisted `SourceAnchor` relation; it does not scan arbitrary repository files.
Requested channels are not reported as successful merely because they were named by a recipe:
each run records availability, applicability, attempted/executed state, raw and eligible counts,
degradation reason, actual reranker mode, fusion weights/K, and stage timings.

Routing Shadow fusion has the `normalized_weighted_rrf_v1` [0,1] contract so downstream consumers
do not receive recipe-dependent raw RRF magnitudes. Context Compiler validates that contract. The
frozen production path intentionally retains `legacy_raw_rrf_v1`; changing its score semantics is
outside this candidate experiment.

The candidate-pool floor/cap (80/1000), rerank window (40), frozen RRF weights/K, and MMR lambda
are named and hash-bound heuristic baselines, not newly justified constants. Routing removes the
incorrect assumption that one channel topology fits every query; it does not manufacture evidence
for the remaining numeric parameters. Those parameters stay in the separate calibration and
promotion track.

Build a profile outside the repository (the script refuses overwrite):

```powershell
.\.venv\Scripts\python.exe scripts\build_retrieval_routing_shadow.py `
  --output D:\MemoryOS-Lab\training\routing\retrieval-routing-shadow-v2.json
```

Run a randomized production-baseline versus routed pair for one registered task:

```powershell
.\.venv\Scripts\python.exe scripts\run_routing_shadow.py `
  --manifest <manifest.json> `
  --runtime <real-agent-runtime.json> `
  --hidden-root <hidden-scorer-root> `
  --profile D:\MemoryOS-Lab\training\routing\retrieval-routing-shadow-v2.json `
  --task-id <registered-task-id> `
  --repeat-id <unique-repeat-id>
```

The runner verifies prompt/runtime parity, exact baseline and candidate config hashes, and every
executed recipe/channel/policy trace. Each evaluation also binds the manifest, task spec, arm run
IDs, and canonical baseline/candidate report SHA-256. It permits only the `memoryos` condition, emits a diagnostic
evaluation rather than a causal weight-training observation, and cannot authorize activation. The
v2 rules are an architecture candidate, not a learned policy. Aggregate completed pairs with:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_routing_shadow.py `
  --profile D:\MemoryOS-Lab\training\routing\retrieval-routing-shadow-v2.json `
  --evaluations <routing-evaluations-1.jsonl> <routing-evaluations-2.jsonl> `
  --output D:\MemoryOS-Lab\training\routing\routing-promotion-decision.json
```

The analyzer canonicalizes and hashes the full evaluation set, rejects duplicate task/agent/repeat
cells, and aggregates repeated agents at task level. Defaults require 50 sealed public tasks, three
repositories, ten sequences, two real agent models, a complete balanced task/agent/repeat matrix,
four observed recipes with five tasks each, a strictly positive paired success lower confidence
bound, no per-task safety regression, nonnegative worst repository/agent/recipe deltas, and complete
latency/cost within budget. A pass is only `eligible_for_sealed_activation_review` and sets
`production_activated=false`.

Replay the candidate and frozen baseline on the same real MemoryOS candidate pools, then compute a
repository-stratified paired bootstrap without rerunning retrieval:

```powershell
.\.venv\Scripts\python.exe scripts\run_public_rrf_shadow_replay.py `
  --dataset D:\MemoryOS-Lab\datasets\swe-gym\SWE-Gym-20260813\train-00000-of-00001.parquet `
  --pyarrow-path D:\MemoryOS-Lab\pydeps `
  --profile D:\MemoryOS-Lab\training\public-bootstrap-v1\SHADOW-swegym-bge-rrf.json `
  --output D:\MemoryOS-Lab\training\public-bootstrap-v1\public-rrf-replay-test-52.json `
  --state-root D:\MemoryOS-Lab\training\public-bootstrap-v1\replay-state `
  --embedding-base-url http://127.0.0.1:8877/v1 `
  --embedding-model BAAI/bge-small-en-v1.5 `
  --split test `
  --queries-per-repository 26

.\.venv\Scripts\python.exe scripts\analyze_public_rrf_shadow_replay.py `
  --report D:\MemoryOS-Lab\training\public-bootstrap-v1\public-rrf-replay-test-52.json `
  --output D:\MemoryOS-Lab\training\public-bootstrap-v1\public-rrf-replay-test-52-analysis-v2.json `
  --bootstrap-rounds 10000 `
  --bootstrap-seed 20260813
```

The 2026-08-13 replay used all 26 Conan test queries and a deterministic 26-query Pandas sample.
The 19.25% FTS / 80.75% vector prior increased repository-macro NDCG@10 by 0.01719 and required
Recall@5 by 0.05769, but the NDCG 95% interval crossed zero (-0.00847, 0.04612), only two test
repositories were available, and Pandas regressed on both metrics. The machine gate therefore
returned `retain_frozen_baseline`. One real pytest coding-agent full/minus-memory pair was helped,
but that establishes the utility of the selected memory, not the causal effect of this weight
ratio. The immutable summary and artifact hashes are recorded in
[`evidence/public-rrf-shadow-v1.json`](evidence/public-rrf-shadow-v1.json).
