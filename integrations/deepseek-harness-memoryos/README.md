# MemoryOS for DeepSeek Harness

Thin, installable DeepSeek Harness bundle with two independent Cordis components:

- `memoryos-usage` records exact successful-response usage and a separate pre-dispatch provider-attempt ledger for both baseline and MemoryOS runs. Retries therefore consume the Harness request ceiling even when no usage object is returned. An optional controller-owned `MEMORYOS_USAGE_GUARD_FILE` is checked synchronously before attempt accounting and provider dispatch. It contributes no prompt text or tool schema.
- `memoryos-tools` always exposes the small `memoryos_control` schema outside
  strict `no_memory`. While enabled, it also exposes `memory_context` and, for
  progressive/delta conditions, `memory_explain`. Its default `read-only` tool
  profile is unchanged.

The split gives the strict `no_memory` baseline zero MemoryOS tool-schema tokens
while preserving the same usage collector in both arms. Ordinary off mode keeps
only the controller so the user can turn MemoryOS back on in chat.
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

Start the MemoryOS HTTP service on the configured port, export its local bearer
token, then install the bundle. An intentional first install starts enabled
unless a persisted switch state says otherwise:

```powershell
$env:MEMORYOS_CONDITION = 'msc_progressive'
$env:MEMORYOS_AUTH_TOKEN = '<local-token>'
./scripts/install.ps1 -Profile memoryos
dsh --profile memoryos --dump-config
dsh --profile memoryos
```

After the first successful `memory_context` result, the Agent receives a
one-time instruction to tell the user that MemoryOS is working. The user can
then type `关闭 OS`, `开启 OS`, or ask `OS 现在开着吗？`. The model must call
`memoryos_control`; disabling dynamically removes every context, explain, and
write tool, while enabling health-checks MemoryOS before restoring them. The
atomic local state survives DSH restarts and contains no memory text or secret.

For a strict baseline, use `MEMORYOS_CONDITION=no_memory`. The tools component
is absent, including `memoryos_control`, so the arm has zero MemoryOS schemas.
`MEMORYOS_ENABLED=0` instead means ordinary off: the control tool remains, and
the model can enable MemoryOS later. The usage component remains active in both
cases. The bundle never changes `agent-default-model`; choose the provider and
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

Version `0.2.0` passed 27/27 contract and real Loader/HMR tests after tarball
installation in a network-disabled RC5 container. The installed-profile test
executes the real disable and health-checked enable calls, verifies dynamic
schema removal/restoration, and separately verifies strict zero-schema
`no_memory` composition.

The install scripts deliberately pack a tarball before calling `dsh plugin add`.
A direct directory install becomes a `link:` dependency, resolves ESM imports
from the source checkout, and bypasses the profile's peer dependency graph, so
it is not a supported deployment form.

## Model experience

Ordinary off mode presents only `memoryos_control`, so it still has a small
schema cost. Strict `no_memory` presents no MemoryOS schema or text. When
enabled, Harness presents the selected schemas; returned context enters later
requests only after a tool call. This intentionally increases input tokens and
can change Provider KV-cache keys. DeepSeek compact mode bounds that increase
but does not claim zero overhead. The usage collector itself is model-invisible
in every condition.

## Known limitations and deferred work

`MEMORYOS_ENABLED` is process-wide. Concurrent enabled and disabled agents should
use separate launches or a future agent-scoped preset, while sharing the same
Harness installation.

Disabling affects subsequent requests. Memory text already present in the
current Session cannot be removed retroactively; start a new Session when a
fully clean context is required. Set a distinct `MEMORYOS_STATE_FILE` for DSH
profiles that need independent switch state.

Remove it with `./scripts/uninstall.ps1 -Profile memoryos` (or the matching
`.sh` script).
