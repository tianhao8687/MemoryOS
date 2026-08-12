# MemoryOS retrieval utility review rubric v1

This pack measures whether a memory candidate would help a coding agent answer or complete the
query at the stated cutoff. It does not ask whether two files merely share a path.

## Blind protocol

1. Use only one file under `blind/` and the matching copied response template. Do not open
   `control/`, the silver qrels, another reviewer's assignment, commit pages, search engines, or the
   target repository while reviewing.
2. Reviewers work independently and do not discuss cases until both response files are frozen and
   hashed.
3. Judge the candidate text and metadata as shown. Do not infer missing facts from a familiar
   project. When the available context cannot support a decision, choose `uncertain` and add
   `insufficient_context`.
4. Complete every candidate row. Candidate order intentionally differs between assignments.

## Semantic relevance

- `0` — no useful evidence for the query, or evidence that would point the agent in the wrong
  direction.
- `1` — topically related background with little effect on the implementation decision.
- `2` — materially useful evidence that narrows the correct implementation, constraint, or file.
- `3` — decisive evidence that is likely to change whether the agent completes the task correctly.

Relevance is about downstream utility, not token overlap. A candidate can mention the same file and
still be `0`; a candidate in another file can be `2` or `3` if it contains the governing decision.

## Safety disposition

- `allow` — the candidate may be retrieved at the stated cutoff and scope.
- `exclude` — the candidate should not be shown even if its text appears relevant.
- `uncertain` — the supplied evidence is insufficient to make a safe allow/exclude decision.

Use issue tags to explain exclusions or uncertainty:

- `future_information`: observed after the query cutoff or otherwise unavailable at that point.
- `wrong_scope`: belongs to another repository or an incompatible task scope.
- `stale`: its validity ended before the query cutoff or newer evidence clearly supersedes it.
- `contradictory`: conflicts with the query's applicable current constraints.
- `misleading`: likely to cause a materially wrong action for another reason.
- `duplicate`: repeats another candidate without adding evidence.
- `insufficient_context`: the pack does not contain enough evidence to decide.
- `other`: a documented concern not represented above.

Git timestamps can be non-monotonic. If timestamp order alone is inconclusive, use `uncertain`; do
not guess ancestry. Temporal and cross-scope guard facts remain separate scorer controls.

## Required retrieval and rationale

Set `must_retrieve=true` only when relevance is at least `2`, disposition is `allow`, and omitting
the candidate would materially weaken the agent's chance of success. A rationale is mandatory for:

- relevance `3`;
- every `must_retrieve=true` decision;
- every `exclude` or `uncertain` decision.

Reviewer confidence describes confidence in the annotation, not MemoryOS's production confidence.
It must never be copied into a truth-mutation threshold.

## Adjudication and gold promotion

An independent adjudicator reviews every pair after both reviews are frozen. The final record must
name both reviewer pseudonyms, explain the resolution, and cover every assigned pair. The dataset
remains `pending_human_adjudication` until this is complete. Even after adjudication, it is
development gold only; production release still requires a sealed external repository test and
real-agent shadow outcomes.
