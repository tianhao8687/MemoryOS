# Release readiness exercise

This repository contains a small operations decision exercise rather than a
code-repair task. Read the current service evidence and the release policy,
then complete `decision/release_plan.json`.

The decision artifact is validated with:

```text
python tests/verify_release_plan.py
```

Use only evidence in this checkout. Historical notes can be stale, and policy
for another service must not influence this release.
