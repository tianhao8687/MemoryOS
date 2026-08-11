# MemoryOS V2.2 real-workload evaluation

V2.2 replaces the old prompt-concatenation A/B demo with a repository-level replay protocol. It
does not by itself prove that MemoryOS improves a coding model. It creates the controls required to
make such a claim testable without exposing private repositories or future solutions.

## What counts as real

There are three dataset tiers:

- `harness_fixture`: local or authored data used only to test plumbing.
- `public_replay`: public licensed repositories, historical tasks, immutable commits, and public
  provenance. This is the default pilot tier.
- `private_opt_in`: explicitly consented private data. The manifest must carry a consent record and
  raw state must remain outside published evidence.

A public replay task is rejected unless it has a 40-character base commit, a later descendant
solution commit, a timezone-aware cutoff between them, an HTTPS task source, repository and license
URLs, a timezone-aware source-publication time no later than the cutoff, a digest-pinned hidden-test
image, and a direct argv test command. Memory evidence must have a capture time no later than the
cutoff. Its source commit must exist before capture; same-repository evidence must also be an
ancestor of the task base. A repository used only for a cross-project guard is validated as memory
provenance even though it intentionally has no task.

Manifest validation is necessary but not sufficient. Before execution, Git history validation
checks commit existence, ancestry, commit timestamps, source timing, valid-time windows, and direct
solution-SHA leakage. The agent checkout is a new repository that fetches only the base and its
ancestors. The mirror containing the future solution is never mounted into the agent container.

## Three paired conditions

Every sampled task runs with the same runtime image, task prompt, base commit, resource limits, and
hidden scorer. Only the MCP configuration changes:

1. `no_memory`: an empty MCP server map.
2. `flat_memory`: a deliberately simple global notebook with lexical ordering and no temporal or
   project isolation.
3. `memoryos`: the real `MemoryService`, Retrieval 2.0 pipeline, context compiler, valid/known-time
   cutoff, repository scope, and persisted `RetrievalRunRow`.

The harness never appends memory text to the prompt. A non-baseline run is invalid unless it has a
successful MCP audit event. The MemoryOS run additionally requires a persisted retrieval run. This
prevents a result from being labeled “MemoryOS-enabled” when the agent ignored MemoryOS.

## Isolation and leakage controls

- The agent runs as a non-root user in a read-only-root container with all Linux capabilities
  dropped, `no-new-privileges`, CPU/memory/PID limits, and bounded logs. It receives four benchmark
  mounts: the sanitized workspace, shared prompt, MCP config file, and one pre-created structured
  result file. A real-agent runtime may additionally declare small read-only credential files under
  `/run/credentials`; their host paths come from named environment variables, are resolved only at
  execution, must be regular non-link files outside the workspace, and are not injected into the
  container environment. Harness stdout/stderr are outside the agent mount, so the agent cannot
  replace them with links or special files. On POSIX, writable disposable binds must match the host
  UID; a root-run harness safely transfers only those generated paths to the declared non-root
  UID:GID.
- The bundled Codex adapter uses a fresh tmpfs `CODEX_HOME`, rejects a repository-root `.codex`
  entry, enables only the isolated `benchmark_memory.memory_context` MCP tool, and approves only a
  pending call whose server, tool, schema, and safe argument shape match the benchmark protocol.
  Exact prompt-derived arguments are checked separately; a safe but non-exact call is allowed to
  finish for auditability but invalidates the sample.
- Codex's nested Linux sandbox currently requires an explicit real-agent-only outer
  `seccomp=unconfined` opt-in. The outer container still runs non-root with a read-only root,
  dropped capabilities, `no-new-privileges`, and resource limits. This exception is acceptable for
  the pinned pilot below, but must be replaced by a tested custom seccomp profile before arbitrary
  public repositories are admitted.
- Memory runs use a separate MCP sidecar. The agent sees only an isolated-network URL; it cannot
  read the flat seed JSON or MemoryOS SQLite database directly.
- Hidden patches are stored outside the agent workspace, verified by SHA-256, and applied only to a
  fresh scoring checkout after the agent patch is captured.
- Agent changes are diffed against the pinned base commit, so an agent-created commit cannot turn
  into an empty patch. Before any post-agent host Git command, the harness rejects changed Git
  config/hooks/info, object alternates, links or special files under `.git`; host Git also runs with
  system/global config, external diffs, text conversion, and hooks disabled.
- Hidden tests run in a digest-pinned container with `--network none`, a read-only root, dropped
  capabilities, and explicit resource limits. Third-party repository code is never executed on the
  Windows host.
- Stale and cross-project guard memories may contain unique canaries. Reports record only the seed
  ID and canary hash, never the raw canary. Any occurrence in the agent patch, message, or bounded
  logs becomes a measured leak or stale-use event.
- Confirmatory mode rejects unrestricted agent internet egress. A cloud-model deployment needs an
  allowlisted model gateway or proxy on the isolated network; broad GitHub access would let the
  agent fetch the hidden solution.

## Evidence and statistics

Each condition record includes functional hidden-test success, protocol validity, patch hash,
memory tool calls, retrieval runs, selected seed IDs, stale/cross-project canary events, latency,
tokens, and cost. The report computes condition aggregates and paired bootstrap 95% intervals for:

- functional success;
- cross-project leaks;
- stale-memory use;
- latency; and
- cost when both paired observations report it.

`dry_run` always emits `effect_claim=none`, even when every task passes. A confirmatory effect claim
is enabled only when all protocol checks pass, including at least 50 distinct tasks, 3 repositories,
10 task sequences, all three conditions per task, identical prompt hashes, registry-qualified image
digests, successful memory usage gates, valid hidden-test setup, complete token/cost accounting, and
no unrestricted agent internet egress. A negative measured result remains a valid result; the
separate MemoryOS safety gate requires zero cross-project canary occurrences. The runtime must also
declare `evidence_type=real_coding_agent`; a deterministic fixture can never unlock an effect claim,
regardless of sample size or image location.

## Commands

Validate the bundled public-history infrastructure smoke with the deterministic fixture adapter:

```powershell
.\.venv\Scripts\python.exe scripts\build_real_workload_fixture_image.py
.\.venv\Scripts\python.exe scripts\real_workload_bench.py `
  --manifest benchmarks\real_workload\public_smoke\manifest.json `
  --runtime build\real-workload\fixture-runtime.json `
  --hidden-root benchmarks\real_workload\public_smoke\hidden `
  --mode dry_run `
  --tasks 1 `
  --run-id markupsafe-public-smoke
```

The fixture image is addressed by its local image ID and declares
`evidence_type=deterministic_fixture`, so it cannot pass confirmatory mode through either gate.
The bundled real Codex adapter can run the pinned one-task public pilot. Build the fixture image
first because it also supplies the MemoryOS MCP sidecar, point the runtime at an existing Codex
authentication file without copying it into the repository, then run all three conditions:

```powershell
$env:MEMORYOS_CODEX_AUTH_FILE = (Resolve-Path "$env:USERPROFILE\.codex\auth.json").Path
.\.venv\Scripts\python.exe scripts\build_real_workload_fixture_image.py
.\.venv\Scripts\python.exe scripts\build_real_workload_codex_image.py `
  --reasoning-effort high
.\.venv\Scripts\python.exe scripts\real_workload_bench.py `
  --manifest benchmarks\real_workload\public_smoke\real_agent_manifest.json `
  --runtime build\real-workload\codex-runtime.json `
  --hidden-root benchmarks\real_workload\public_smoke\hidden `
  --output-root build\real-workload\evidence `
  --mode dry_run `
  --tasks 1 `
  --run-id markupsafe-codex-real-paired
```

Use `--condition` only for dry-run calibration. A confirmatory run always requires all three
conditions. For any other real coding agent, supply a runtime JSON with registry-qualified agent
and MCP image digests, an argv command containing `{workspace}`, `{prompt_file}`, `{mcp_config}`,
and `{result_file}`, and environment variable names (not secret values). Loading a runtime file does
not require those variables; execution fails before creating a Docker network if any are missing.
Runtime evidence records the non-root agent, MCP, and scorer UID:GID plus resource limits. On POSIX,
generated runtime defaults match the non-root host UID:GID so disposable bind mounts remain
writable. The adapter must write this JSON object:

```json
{
  "status": "completed",
  "input_tokens": 1000,
  "output_tokens": 250,
  "cost_usd": 0.0123,
  "tool_calls": 8,
  "message": "optional bounded summary"
}
```

Raw run state under `build/real-workload/run-state/` can contain public memory, patches, and agent
logs and is not a publishable artifact. Reports default to `docs/verification/v2.2/<run-id>/`; the
pilot command above deliberately keeps its report under `build/real-workload/evidence/`. Review any
report before publishing, especially for `private_opt_in` runs.

## Pinned real-agent pilot result

The 2026-08-11 MarkupSafe replay completed with protocol-valid evidence in all three conditions.
All three patches passed the semantic hidden test. MemoryOS made one MCP call, persisted one
retrieval run, and selected only `warning-category-decision`; flat memory selected both that useful
record and the stale `old-warning-category` record. No stale canary or cross-project canary reached
the patch, message, or bounded logs.

This is a plumbing and safety result, not an effect result. With one task, functional success was
1.0 for every condition. MemoryOS took 84.96 seconds, flat memory 82.11 seconds, and no memory
101.34 seconds; flat memory also used fewer input tokens than MemoryOS. These single observations
have no useful confidence interval, provider cost was unavailable under ChatGPT authentication,
and repeated calibration already showed patch-shape variance. The report therefore correctly says
`effect_claim=none`.

## Honest remaining boundary

The deterministic fixture remains plumbing-only; the bundled Codex adapter now provides one real
coding-agent pilot. It is still unsuitable for a confirmatory claim or arbitrary untrusted
repositories: the pilot uses unrestricted model internet egress, a directly mounted long-lived
authentication file, local image IDs, an outer unconfined seccomp exception, and no provider cost
meter. Before scaling, add a model-only egress gateway with short-lived credentials, replace the
seccomp exception, publish registry-qualified image digests, and collect cost. Then run an
exploratory 10-15-task corpus across at least three repositories before deciding whether the
50-task confirmatory study is justified. Until a confirmatory sample passes, MemoryOS makes no
real-agent effect claim.
