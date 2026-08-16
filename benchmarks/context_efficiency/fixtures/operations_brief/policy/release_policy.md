# Release policy

For this service, a release is ready only when all current gates pass:

- p95 request latency must be at most 250 ms;
- error rate must be at most 1.0%;
- evidence completeness must be 100%.

If latency is the only failed gate, set:

- `decision` to `hold`;
- `reason_code` to `latency_slo_breach`;
- `max_canary_percent` to `5`;
- `required_action` to `rerun_latency_benchmark` after remediation.

The decision artifact must also name `evidence/current_metrics.md` as its
`evidence_file`. Policies from other services are out of scope.
