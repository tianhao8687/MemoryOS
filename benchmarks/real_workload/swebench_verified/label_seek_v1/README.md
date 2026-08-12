# Label-seeking real-agent ablations

This frozen batch adds four SWE-bench Verified tasks from the two training repositories that did
not yet produce executable calibration labels: Pylint and pytest. The batch is deliberately
enriched for tasks where a temporally valid project memory can shorten architecture discovery.
That enrichment is appropriate for training-data acquisition, but it is not an effectiveness
estimate; unbiased claims remain reserved for the sealed promotion set.

The dataset revision and Parquet digest are inherited from the checked-in cross-repository pack.
Repository partitions remain immutable: Pylint and pytest are `train`, while Seaborn remains the
repository-held-out `dev` partition. Selection, prompts, memories, hidden scorers, runtime policy,
and arm randomization are frozen before any arm in this batch runs.

The batch uses the pinned `runtime-terra-medium.json`: an everyday balanced coding-agent model,
medium reasoning, and a 14-minute agent budget inside a 15-minute container limit. This bounded
runtime is intentional because project memory should be evaluated as a way to reduce search under a
realistic delivery budget, not only with the strongest model and an effectively unbounded session.

Every memory summarizes code or repository behavior already present at the task cutoff. The agent
never receives the SWE-bench gold patch, official tests, solution commit, scorer source, or outcome
of the other arm. Hidden scorers run without network access and must fail on the pinned base while
passing on the official solution commit before the task is eligible to run.

Only a protocol-valid pair whose full and minus arms have different executable outcomes creates a
causal training observation. Same-outcome pairs remain replication evidence and are never relabeled
from latency or subjective patch quality.

## Post-run audit

The frozen batch completed, but its post-run scorer audit invalidated three of four pairs. Two raw
discordant observations were emitted and are explicitly ineligible: one scorer omitted an existing
runtime field, and another overconstrained an equivalent lazy-loading architecture. A third scorer
failed to carry an agent-added helper into its isolated dependency closure. Only Pylint 4661 remains
a valid same-outcome pair, and this batch contributes zero training labels. See
`post-run-audit.json` for bound patch/evidence hashes and infrastructure incidents.
