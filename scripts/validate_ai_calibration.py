from __future__ import annotations

import json
from pathlib import Path

from memoryos.evaluation.ai_calibration_protocol import validate_ai_calibration_assets


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    readiness = validate_ai_calibration_assets(root)
    summary = {
        "status": readiness.status,
        "protocol_sha256": readiness.protocol_sha256,
        "evidence_artifacts": len(readiness.evidence),
        "effective_jury_model_families": readiness.gates.effective_jury_model_families,
        "effective_jury_providers": readiness.gates.effective_jury_providers,
        "real_agent_ablation_pairs": readiness.gates.real_agent_ablation_pairs,
        "sealed_promotion_tasks": readiness.gates.sealed_promotion_tasks,
        "production_weights_frozen": readiness.production_weights_frozen,
        "production_profile_active": readiness.production_profile_active,
        "blockers": readiness.blockers,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
