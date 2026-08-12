# Model adjudication policy (frozen before opening reviewer decisions)

Status: provisional model adjudication only; never human gold.

1. Adjudicate only after both independent response files are frozen and validated. Do not consult
   silver qrels, `control/source_map.jsonl`, repositories, or the network until final decisions are
   frozen.
2. Judge semantic relevance independently from safety. Exact words or paths are not sufficient:
   `3` requires decisive actionable evidence, `2` materially constrains implementation, `1` is
   related background, and `0` is non-useful or misleading.
3. Set `must_retrieve=true` only for an allowed relevance-2/3 candidate whose omission would
   materially weaken task success; repeated or merely topical evidence is not required.
4. A different repository is `exclude/wrong_scope`. An explicit validity window ending before the
   cutoff is `exclude/stale`. For Git-history candidates, timestamp order alone cannot establish
   ancestry; where it is the only safety signal, use `uncertain/future_information` rather than
   inventing ancestry. Otherwise allow absent visible safety evidence.
5. For adjacent relevance disagreements, select the higher grade only when the candidate contains
   an actionable constraint, mechanism, or directly reusable change; otherwise select the lower.
   Inspect every non-adjacent disagreement individually.
6. Safety takes precedence over `must_retrieve`: excluded or uncertain candidates can never be
   required. Every disagreement resolution must state evidence from the blind packet rather than
   simply choosing reviewer A or B.
7. After final adjudication is hashed, silver comparisons may be computed only as a diagnostic.
   They cannot retroactively alter the frozen decisions.
