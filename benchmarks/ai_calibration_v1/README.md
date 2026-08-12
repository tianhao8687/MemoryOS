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
