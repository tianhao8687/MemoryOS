# Adaptive scorer-hardened label acquisition

This batch is an explicit adaptive training-data exercise, not an effectiveness benchmark. It
repeats two hard tasks whose earlier pairs were invalidated by scorer defects. The new scorers are
frozen before these arms run and must pass base/solution separation plus known-equivalent
implementation checks derived from the invalidation audit. Prior agent patches are used only to
test scorer equivalence and are never shown to the new agents.

Pylint and pytest remain in the immutable `train` partition. Each task has two predeclared paired
repeats with balanced first-arm order. Repeated observations will not be treated as independent
tasks: a task-level label is eligible only if discordant repeats agree in direction; inconsistent
or single-repeat effects remain diagnostic.

The Pylint memory adds a cutoff-valid dependency API constraint that was missing from the earlier
architecture memory. The pytest memory is unchanged; only its scorer isolation is repaired. This
adaptive enrichment can create training evidence but cannot support product-effect claims or
replace the sealed repository/model promotion gate.
