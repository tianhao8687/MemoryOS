# Cross-session long-term memory acceptance v1

This benchmark separates three claims that must not be conflated:

1. MemoryOS core data survives a client and service restart.
2. A fresh DeepSeek session can retrieve a confirmed record from the same scope.
3. A DeepSeek source session can create that confirmed record through the
   integration, without controller pre-seeding or transcript replay.

Only claim 3, followed by successful claim 2 retrieval, is an end-to-end Agent
long-term-memory result. Pre-seeding a record proves retrieval but does not prove
that one Agent session remembered a conversation for another session.

## Live protocol

The three frozen cases are defined in `cases.json`: a project decision, a
project constraint, and a previously failed approach. Each source session has
six ordinary development turns containing durable facts, temporary details,
repository-readable observations, and unresolved choices. Source prompts may
not mention remembering, MemoryOS, or any MemoryOS tool.

The source model must independently propose and confirm the durable facts
through model-visible MemoryOS integration tools. The controller then terminates
the Harness session, terminates and restarts the MemoryOS service while
preserving only its data directory, and starts three retrieval arms with new
Harness session ids, homes, workspaces, and cold provider-cache namespaces:

- A: MemoryOS disabled, same repository scope.
- B: MemoryOS enabled, same repository scope.
- C: MemoryOS enabled, different repository scope.

All retrieval arms for a case receive exactly the same question and never
receive the source transcript. B must recover every required durable fact after
calling MemoryOS. A and C must not recover the complete hidden decision, and C
must not receive the target memory. The source transcript, source Harness
session store, run ids, file names, environment variables, prompts, and
workspaces exposed to retrieval arms must not contain the hidden facts outside
the persistent MemoryOS database.

The three source cases may run concurrently because each uses an independent
persistent store. After the three hard restarts, all nine retrieval sessions may
also run concurrently. The V1 gate is 3/3 cases with no provider retries or
automatic repetitions.

## Frozen live result

The latest frozen campaign is `live-r5`, using `dsh-memoryos 0.1.15`,
`deepseek-v4-flash`, three parallel source sessions, nine parallel retrieval
sessions, zero provider retries, and zero automatic repetitions. The strict
campaign gate is **failed (2/3)** and is not relabeled.

All three same-scope B arms recovered the required history after a hard service
restart, all three no-memory A arms abstained on history-only facts, and all
three wrong-scope C arms remained isolated. Two source cases passed every gate.
The database source case created six confirmed records, including semantically
correct English records for “MySQL no longer maintained” and “compatibility kept
until v3”; its source gate nevertheless required the Chinese lexical terms
`不再/维护/兼容` and marked those two facts absent. That scorer-language mismatch
explains the remaining strict failure but does not change the recorded result.

The campaign made 141 provider attempts with 0 retries and recorded 1,874,280
input, 65,895 output, and 31,411 reasoning tokens. The scale is a warning as
well as evidence: unconstrained multi-turn source writing is expensive. The
smaller follow-up update and eviction protocol therefore isolates one claim per
test and is documented in
[`../long_term_memory_followup_v1/README.md`](../long_term_memory_followup_v1/README.md).

## Live implementation

The core restart and scope-isolation test is
`tests/test_mcp_stdio.py::test_real_stdio_cross_session_restart_and_scope_isolation`.
The DeepSeek plugin keeps its existing `read-only` default and adds the explicit,
evaluation-only `cross-session-write` profile for Session A. That profile adds
only `memory_propose` and `memory_confirm`, with repository scope fixed by the
controller. `scripts/run_cross_session_memory_v1.py` runs the three source
sessions concurrently, performs a real MemoryOS HTTP process restart per case,
then runs all nine fresh retrieval sessions concurrently with no automatic
repetition and provider retries set to zero.

The root-cause record for the earlier broad-record/conflict storm is
[`plugin-optimization-v0.1.14.md`](plugin-optimization-v0.1.14.md). It records
the failure before the fix and must not be read as a claim that the later strict
3/3 gate passed.
