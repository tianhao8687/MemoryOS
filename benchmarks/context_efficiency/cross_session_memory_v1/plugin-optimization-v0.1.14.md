# DeepSeek Harness MemoryOS plugin 0.1.14 root-cause fix

Date: 2026-08-17

This note is a post-run engineering record. It does not modify the frozen
`live-r4` acceptance result or claim a new DeepSeek A/B/C outcome.

## Root cause

The database case was not blocked by task difficulty or networking. The failure
was a causal chain across the model-visible plugin contract:

1. `memory_propose` allowed one record to contain several independently changing
   decisions and did not require a stable semantic key. PostgreSQL 17, Alembic,
   and the MySQL direction were therefore merged into one broad record.
2. When the later MySQL maintenance decision conflicted with that record,
   MemoryOS correctly returned `CONFLICT_DETECTED` and the supported resolution
   path. The core client could send `strategy`, but `memory_confirm` did not show
   that parameter to the model.
3. The client reduced non-2xx responses to a bare HTTP status. Through the local
   Harness bridge, the MemoryOS error was additionally nested inside a bridge
   error. The model could not see the stable error code, candidate id, conflict
   ids, or allowed strategies.
4. The plugin kept no pending-conflict state. The Agent could repeatedly confirm
   without a strategy and create replacement candidates. In `live-r4`, this
   produced five 409 confirms, repeated semantically equivalent proposals, 12
   provider responses, and 13 memory events in one source turn.
5. Retrieval later compiled only part of the original broad record. The B arm
   recovered PostgreSQL and the v3 boundary but omitted Alembic and the
   stop-maintaining-MySQL decision.

The payment case's frozen failure is separate: its memory and B-arm answer were
semantically correct, while a lexical scorer required `试过` and rejected the synonym
`已尝试`. That is an evaluation false negative, not this plugin failure.

## Plugin changes

Version `0.1.14` changes only the explicit evaluation write profile:

- `memory_propose` now requires `key` and tells the Agent to submit exactly one
  independently updateable fact per candidate.
- `memory_confirm` now exposes `supersede`, `keep_both`, and `reject` in the
  actual model-facing JSON Schema.
- Direct MemoryOS errors and errors nested by the Harness bridge retain their
  stable code, message, and structured details.
- A conflict becomes an actionable normal tool result containing the candidate
  id, conflict ids, strategy guidance, and the exact next action.
- Per-session pending-conflict state blocks a second candidate and suppresses
  repeated strategy-free confirmation requests until the original candidate is
  resolved.
- The evaluation bridge rejects proposals without a stable atomic-fact key.
- The default `read-only` profile and its model-visible tools are unchanged.

This removes the deterministic candidate/request storm and reduces the chance
that future records lose facts during compilation. It does not retroactively
split existing broad memories, and it does not by itself prove a higher live
model success rate.

## Offline verification

No DeepSeek provider request was made for this verification.

- Packaged and installed `dsh-memoryos@0.1.14` into the isolated profile
  `D:\DeepSeek\dsh-plugin-v014-loader`.
- Used the locked image
  `memoryos-deepseek-lab@sha256:32359ca8e79047f42582daf6153af39a4a7c2e60e0a88fa6529d5a0bb6beff2d`
  with Docker networking disabled.
- Node contract and installed Loader/HMR tests: 21 passed.
- Python bridge, runner, and real cross-session restart tests: 3 passed.
- Ruff: passed. Mypy: passed. `git diff --check`: passed.

The installed Loader test checks the final Schema exposed to the model, not only
the source definition: proposal `key` is required and confirmation includes all
three conflict strategies.

Version `0.1.15` adds request-evidence-only token attribution for the follow-up
live run. It does not change prompts or model-visible tool behavior. The two
MemoryOS component counts use the declared `unicode-heuristic-v1` estimate;
whole Session A input remains the exact provider-reported value.

## Remaining proof

A new live run is still required to measure Agent behavior after the fix. It
must use fresh repositories, sessions, Harness homes, provider cache namespaces,
and MemoryOS data. The frozen `live-r4` result remains 1/3 by its lexical gate
(2/3 by the documented semantic reading); it must not be relabeled as a
0.1.14 result.
