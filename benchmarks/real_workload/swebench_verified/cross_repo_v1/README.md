# Cross-repository real-agent ablations

This frozen public-replay pack adds three SWE-bench Verified tasks from three repositories. It is
designed to supplement, not replace, the Requests proxy-auth pilot.

The repository-level split was locked before any agent arm ran: Pylint and pytest are training
repositories, while Seaborn is development-only. Together with the existing Requests training
task, this reaches the calibration protocol's three-training-repository topology without allowing
a task to move partitions after its outcome is known.

Each memory is reconstructed from repository state that existed at or before the task cutoff and
is pinned to an ancestor commit. The task checkout contains neither the official solution object
nor the gold patch. The hidden scorers use only the Python standard library, run without network
access, and exercise behavior through dependency shims or isolated AST execution. All three
scorers fail at the pinned base and pass at the official merge commit.

The full/minus-memory outcomes are still real-agent weak evidence. A task creates a causal training
observation only when both arms are protocol-valid and their executable outcomes disagree. A
same-outcome pair remains useful replication evidence but is not converted into a label. These
tasks do not satisfy the independent three-provider AI-jury gate or authorize production weights.
