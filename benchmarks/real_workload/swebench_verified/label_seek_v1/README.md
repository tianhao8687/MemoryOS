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

Every memory summarizes code or repository behavior already present at the task cutoff. The agent
never receives the SWE-bench gold patch, official tests, solution commit, scorer source, or outcome
of the other arm. Hidden scorers run without network access and must fail on the pinned base while
passing on the official solution commit before the task is eligible to run.

Only a protocol-valid pair whose full and minus arms have different executable outcomes creates a
causal training observation. Same-outcome pairs remain replication evidence and are never relabeled
from latency or subjective patch quality.
