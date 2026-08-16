# DeepSeek V4 Flash medium A/B/C v2

This campaign uses the real `pytest-pr-10356` task from the frozen
`label_seek_v1` manifest. It compares `no_memory`, `msc_full`, and
`msc_progressive` in three isolated cold workspaces.

The model adapter and its prompt are identical across all three arms. This
campaign does not add an inspection-count deadline or any task-specific hint.
Only MemoryOS tool exposure and the plugin response returned to the model may
differ.

The C arm uses the plugin's `deepseek-progressive-compact` renderer. It keeps
both progressive tools but removes volatile JSON metadata and redundant explain
arguments from the model-visible surface. B remains the full JSON condition.

The controller-owned relative guard stops an unfinished arm before its next
provider dispatch when input tokens, provider attempts, or cost exceed 1.30
times both terminal peers. Existing absolute per-arm limits remain active and
the relative limit is never expanded automatically.

Hidden-test workspaces are materialized under the controller root, outside all
three agent-visible condition roots.
