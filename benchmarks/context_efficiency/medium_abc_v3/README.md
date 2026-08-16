# DeepSeek V4 Flash medium A/B/C v3

This campaign uses the frozen `pylint-pr-4661` task from `label_seek_v1`.
It is a different repository area and problem shape from the earlier Pylint
recursive-discovery and pytest mark-MRO campaigns.

All three arms use the same `deepseek-v4-flash` runtime, `standard-offline`
agent preset, reasoning level, task prompt, starting commit, parallel start,
and isolated hidden scorer. No inspection-count deadline or forced-edit prompt
is added.

- A: `no_memory`
- B: `msc_full`
- C: `msc_progressive` with MemoryOS plugin `0.1.9`

Plugin `0.1.9` changes only the progressive MemoryOS surface: it compacts the
index status, removes repeated verification wording, preserves local source
anchors when present, deduplicates repeated evidence, normalizes unknown
freshness, and supplies one bounded verification boundary for a multi-clause
contract.

The controller stops an unfinished arm before its next provider dispatch when
its exact input tokens, provider attempts, or cost exceed 1.30 times both
terminal peers. There are no retries, automatic repetitions, or automatic
budget expansions.
