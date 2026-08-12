# MemoryOS Git Silver Calibration Dataset V1

This dataset is the first data-backed retrieval calibration input for MemoryOS. It mines pinned,
public Git histories without executing upstream code and stores only commit subjects, changed paths,
timestamps, and source links. It does not redistribute source files or diffs.

## What it can and cannot establish

The labels are **silver labels**, not human relevance judgments. A future task is proxied by a target
commit subject, while candidate memories are older commit subjects plus changed paths. Relevance is
derived independently from hidden path overlap:

- `3`: exact changed path overlap; the nearest exact-path ancestor is marked `required`.
- `2`: at least one shared immediate parent directory.
- `1`: a shared top-level path and file suffix.
- `0`: no structural relationship above.

Every query also contains a same-repository future guard and a candidate from a dedicated guard-only
repository. Both are `forbidden`. This makes temporal leakage and cross-scope leakage hard gates
rather than tunable preferences.

The future guard is determined by first-parent ancestry, not wall-clock order. A small number of real
histories contain non-monotonic commit timestamps on purpose; consumers must use Git ancestry for the
hard temporal filter instead of assuming a timestamp comparison is sufficient.

This dataset is appropriate for candidate-pool, lexical/vector retrieval, temporal/scope filters,
reranker, and diversity experiments. It does not contain an adjudicated claim graph, so it cannot by
itself calibrate the graph channel. It is also not evidence for truth-conflict confidence thresholds,
memory health weights, or downstream agent effectiveness. Those require separately adjudicated and
longitudinal datasets.

## Repository-held-out splits

| Split | Repositories | Languages |
|---|---|---|
| train | MarkupSafe, HTTPX, Express, Vite | Python, JavaScript, TypeScript |
| dev | Tokio Bytes | Rust |
| test | lazygit | Go |
| guard only | ItsDangerous | Python |

All repositories, snapshots, source pages, and license pages are pinned in `sources.json`. The guard
repository never contributes a query. Test labels are public, so this is a held-out confirmatory split,
not a secret benchmark.

## Artifact isolation

Generated artifacts are under `data/`:

- `candidates.jsonl`: runtime candidate records shared by all splits.
- `<split>/queries.jsonl`: runtime queries and candidate IDs; no target commit, path labels, relevance,
  or eligibility is present.
- `<split>/qrels.jsonl`: scorer-only relevance, eligibility, and target-path evidence.
- `manifest.json`: source snapshots, license hashes, artifact SHA-256 values, counts, policy, and known
  limitations. It also records a normalized SHA-256 over the generator's two source modules.

`load_runtime_split()` deliberately opens candidates and queries without opening qrels. Full dataset
validation checks hashes, row counts, references, split ownership, required positives, both guard
types, and target-commit leakage.

## Rebuild

From the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\build_calibration_dataset.py
.\.venv\Scripts\python.exe scripts\validate_calibration_dataset.py
```

After the first build, a network-free reproducibility check is available:

```powershell
.\.venv\Scripts\python.exe scripts\build_calibration_dataset.py --offline `
  --output-dir build\calibration-v1\offline-rebuild
```

The checked-in source configuration fixes `generated_at`; rebuilding the same Git objects therefore
produces byte-identical JSONL artifacts and the same manifest digest.

## Human adjudication path

Silver cases should be sampled for two-reviewer annotation, with disagreements adjudicated. Human
labels must use `origin=human_adjudicated` and `review_status=adjudicated`, and should be published as
a new dataset/profile rather than silently overwriting this immutable silver baseline.

The runtime-only train/dev inputs now feed the separate blind review pilot in
[`../human_review_v1/README.md`](../human_review_v1/README.md). Its builder never opens this dataset's
qrels and never samples the sealed test split.
