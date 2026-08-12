# Provisional model-only review

This directory records a completed two-reviewer plus third-party-adjudicator exercise over the
blind `memoryos-human-review-v1-pilot` pack. It is an auditable **model-only** label tier, not the
missing human-gold dataset and not evidence that production retrieval weights are calibrated.

The effective reviewers covered all 1,922 candidate pairs under different blind assignments. A
third model role adjudicated all 527 disagreements under a policy frozen before opening either
decision file. The initial reviewer-A attempt was invalidated after it self-reported using a
project-wide code search outside the allowed three files; none of that attempt's data was used, and
a replacement reviewer completed the assignment. The exact incident and the limits of procedural
independence are preserved in [`protocol-audit.json`](protocol-audit.json).

Key results:

- relevance exact agreement: 1,455/1,922 (75.70%);
- relevance Cohen's kappa: 0.203 (linear-weighted 0.322, quadratic-weighted 0.424);
- safety exact agreement: 1,877/1,922 (97.66%), kappa 0.834;
- full relevance/safety/must-retrieve agreement: 1,395/1,922 (72.58%);
- final provisional labels: relevance 0/1/2/3 = 1,608/178/92/44;
- post-adjudication exact agreement with Git path-overlap silver labels: 60.99%.

The lower chance-adjusted relevance agreement is the important result: the dominant relevance=0
class inflates raw agreement, and visible downstream utility remains materially subjective. The
silver comparison was opened only after the adjudication hash was fixed. Its divergence shows why
path overlap cannot be promoted to human gold; it does not prove that these model labels are truth.

Validate the complete chain from the two reviews through disagreement extraction, decision plan,
527 resolutions, final 1,922-row adjudication, hashes, distributions, and silver diagnostic:

```powershell
.\.venv\Scripts\python.exe scripts\validate_model_review.py
```

The canonical metrics and artifact hashes are in [`report.json`](report.json). The final artifact is
[`adjudicated-provisional.jsonl`](adjudicated-provisional.jsonl), SHA-256
`0c836306283ae750521b5526b844c8b0a0ef6c0a05137bcb5f846dcd558e83e1`. Keep the original human
review pack at `pilot_unlabeled`; completing it as human gold would require two actual humans and an
independent human adjudicator. Production calibration instead follows the separate
[`../../ai_calibration_v1/`](../../ai_calibration_v1/) AI-only executable protocol. This artifact
does not meet that protocol either: all roles came from one effective model family, judgments were
not order-swapped pairwise comparisons, and no real-agent full/minus ablation was run.
