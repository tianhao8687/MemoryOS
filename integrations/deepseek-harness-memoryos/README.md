# MemoryOS for DeepSeek Harness

Thin, installable DeepSeek Harness bundle with two independent Cordis components:

- `memoryos-usage` records exact successful-response usage and a separate pre-dispatch provider-attempt ledger for both baseline and MemoryOS runs. Retries therefore consume the Harness request ceiling even when no usage object is returned. An optional controller-owned `MEMORYOS_USAGE_GUARD_FILE` is checked synchronously before attempt accounting and provider dispatch. It contributes no prompt text or tool schema.
- `memoryos-tools` exposes `memory_context` and, for progressive/delta conditions, `memory_explain`. It is mounted only when `MEMORYOS_ENABLED=1`. Its default `read-only` tool profile is unchanged.

The split gives the `no_memory` baseline zero MemoryOS tool-schema tokens while preserving the same usage collector in both arms.
It targets Harness `0.1.0-rc.5` at commit
`47f943859bef60e4160492346772ded9b24f765a` and fails loudly when the required
tool/event surfaces are absent.

For `msc_progressive`, the DeepSeek adapter selects
`deepseek-progressive-compact`. The plugin still exposes both `memory_context`
and `memory_explain`. When the index contains exactly one resolved record,
`memory_context` expands it inside the local MemoryOS call and returns one
action-ready contract; the model does not need a separate synthesis call. A
multi-record or unresolved index keeps selective `memory_explain`, whose schema
is reduced to the exact `UUID @ SHA256` handle shown in the index. Both paths
remove volatile ids and accounting metadata, preserve repository-local anchors,
normalize contradictory unknown freshness, and keep implementation choices
separate from resolved behavioral constraints.

The action-ready contract also declares that external lookup is unnecessary,
provides a validation fallback for offline or dependency-limited workspaces, and
defines when investigation should be reopened. After a resolved contract and a
successful local inspection, the plugin may add one generic recovery notice if
a tool reports an offline/missing-dependency failure while `git status` still
shows a clean worktree. The notice is emitted at most once per session. It has
no fixed step number and contains no repository-, task-, dependency-, provider-,
or model-specific answer.

Start the MemoryOS HTTP service with the compiler selected for the experiment,
export its local bearer token, enable the tool component, then install the bundle:

```powershell
$env:MEMORYOS_ENABLED = '1'
$env:MEMORYOS_CONDITION = 'msc_progressive'
$env:MEMORYOS_AUTH_TOKEN = '<local-token>'
./scripts/install.ps1 -Profile memoryos
dsh --profile memoryos --dump-config
dsh --profile memoryos
```

For the baseline, use the same installation and launch configuration with
`MEMORYOS_ENABLED=0` and `MEMORYOS_CONDITION=no_memory`. The usage component
remains active; `memory_context` and `memory_explain` are absent from the model's
tool list. The bundle never changes `agent-default-model`; choose the provider and
model in Harness itself or in the benchmark runtime.

The example configuration contains no provider or MemoryOS secret. The plugin
reads the local MemoryOS token only from the configured environment variable or
token file. SQLite, migrations, retrieval, Truth, and compilation remain in the
MemoryOS service; this bundle does not duplicate them.

The evaluation-only cross-session source profile is enabled explicitly with
`MEMORYOS_TOOL_PROFILE=cross-session-write`. It adds only `memory_propose` and
`memory_confirm`, fixes every proposal to the configured repository scope, and
requires conversation evidence. The default remains `read-only`; existing
baseline, context-only, progressive, and delta launches gain no write schemas.

Each proposal in the write profile must use a stable semantic key and contain
one independently updateable fact. If confirmation reports a conflict, the
plugin keeps that candidate pending, exposes the supported `supersede`,
`keep_both`, and `reject` strategies, and blocks replacement proposals until
the same candidate is resolved. Structured MemoryOS error codes and conflict
ids remain visible to the Agent instead of being reduced to a bare HTTP status.

Provider-attempt evidence also records an estimated write-token attribution for
the final DeepSeek-visible request. `write_tool_schema_tokens` counts one copy
of the `memory_propose` and `memory_confirm` schemas in that request;
`memory_write_visible_tokens` adds write-tool results already replayed on the
visible conversation surface. The counter is explicitly identified as
`unicode-heuristic-v1`; exact provider input remains sourced only from provider
usage.

For the bounded DeepSeek coding profile, use `msc_context_only` with:

```text
MEMORYOS_BUDGET_TOKENS=512
MEMORYOS_MAX_CONTEXT_CALLS=1
MEMORYOS_RESPONSE_FORMAT=deepseek-compact
```

Because task and repository are fixed by the adapter, this mode exposes an
argument-free `memory_context` schema, permits one call, and returns only the
context text plus a short verification reminder. It does not expose
`memory_explain` or experiment metadata to the model.

Run the keyless contract test with:

```console
npm test
```

The real Loader/HMR test uses an installed profile. Set
`DSH_TEST_PROFILE_DIR` to that profile directory before running
`npm run test:loader`.

The install scripts deliberately pack a tarball before calling `dsh plugin add`.
A direct directory install becomes a `link:` dependency, resolves ESM imports
from the source checkout, and bypasses the profile's peer dependency graph, so
it is not a supported deployment form.

## Model experience

When disabled, the bundle adds no model-visible text or tools and therefore no
MemoryOS input-token overhead. When enabled, Harness presents the selected
MemoryOS tool schemas; returned context enters later requests only after a tool
call. This intentionally increases input tokens and can change provider KV-cache
keys. The DeepSeek compact mode bounds that increase but does not claim zero
overhead. The usage collector itself is model-invisible in both conditions.

## Known limitations and deferred work

`MEMORYOS_ENABLED` is process-wide. Concurrent enabled and disabled agents should
use separate launches or a future agent-scoped preset, while sharing the same
Harness installation.

Remove it with `./scripts/uninstall.ps1 -Profile memoryos` (or the matching
`.sh` script).
