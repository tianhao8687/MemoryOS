# Long-term memory follow-up v1

This frozen follow-up tests two failure modes that ordinary “remember and
recall” demos miss: replacing old truth and recovering a decision only after it
has left the active conversation context.

## Protocol

### Memory Update Test

Session A establishes PostgreSQL 17 through the model-visible write tools. The
MemoryOS process is stopped and restarted while preserving only its data
directory. A fresh Session B explicitly replaces 17 with PostgreSQL 18 and must
resolve the detected conflict with `supersede`. After another hard restart, a
fresh Session C asks only for the current production database version.

The gate requires the old record to be `superseded`, the new record to be
`active`, the replacement link to point to the old record, the current context
to exclude the superseded 17 record, and Session C to answer 18 only.

### Context Eviction Test

Both arms use one continuous DSH session, the same frozen prompts, an 8,192-token
declared context window, and a 3,500-character controlled active-history limit.
The first turn establishes the release codename `Glacier-47`, followed by three
large batches of normal development content. The runner proves that the
sentinel-containing source turn was shadowed and the retained active history no
longer contains the sentinel before final recall.

The no-memory arm has no MemoryOS tools on the final turn and must answer
“不知道”. The MemoryOS arm may call `memory_context` only for final recall and
must recover `Glacier-47`. Controlled eviction is evaluation-only and is not a
production conversation policy.

## Frozen live-r4 result

Overall status: **PASS**. The run used `dsh-memoryos 0.1.18`,
`deepseek-v4-flash`, the locked Linux/Docker DSH environment, and zero provider
retries.

| Test | Result | Core observation |
|---|---:|---|
| Memory Update | PASS | PG17 active=0/superseded=1; PG18 active=1; fresh C answered PG18 only |
| Context Eviction | PASS | No Memory=`不知道`; MemoryOS recovered `Glacier-47` after verified eviction |

Every one of the 19 update gates and 21 eviction/cross-arm gates passed. There
were 24 provider attempts and 24 completed responses: 214,165 input, 3,743
output, and 2,206 reasoning tokens, with a frozen-price cost of approximately
USD 0.0027042792.

## Write-token accounting

| Write session | `write_tool_schema_tokens` | `memory_write_visible_tokens` | `provider_input_tokens` |
|---|---:|---:|---:|
| `memory_update_session_a` | 598 | 2,593 | 34,061 |
| `memory_update_session_b` | 598 | 2,593 | 35,287 |
| `context_eviction_memoryos_session_a` | 598 | 2,593 | 34,339 |
| **Total** | **1,794** | **7,779** | **103,687** |

`write_tool_schema_tokens` is a one-copy estimate of the write schemas visible
in that session. `memory_write_visible_tokens` is the cumulative estimated
write-schema plus replayed write-result attribution across provider requests.
Both use `unicode-heuristic-v1`. `provider_input_tokens` is the exact Session A/B
input total returned by the provider; the component estimates must not be
presented as exact DeepSeek tokenizer counts.

## Fixes exercised by this run

- Stable atomic write keys and model-visible `supersede/keep_both/reject` paths.
- Structured conflict errors and per-session pending-conflict state.
- Active-only CJK retrieval with a bounded n-gram fallback.
- Context atoms that preserve the write key and confirmed source content.
- A true model-surface eviction event rather than merely adding filler below a
  large provider context window.
- Separate Provider-exact totals and estimated MemoryOS component attribution.

The machine-readable frozen summary is
[`live-r4-summary.json`](live-r4-summary.json). Cases and runtime locks are in
this directory. Run from the repository root with:

```powershell
.\.venv\Scripts\python.exe scripts\run_long_term_memory_followup_v1.py --help
```

This two-case pass validates the stated mechanisms. It is not a general coding
success-rate result and does not erase failures from the larger cross-session or
A/B/C campaigns.
