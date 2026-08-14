from __future__ import annotations

import json
from pathlib import Path

from scripts.sync_project_status import (
    READINESS_END,
    READINESS_START,
    sync_documents,
)


def test_readiness_sync_detects_drift_and_write_repairs_it(tmp_path: Path) -> None:
    readiness = tmp_path / "readiness.json"
    readiness.write_text(
        json.dumps(
            {
                "status": "evidence_pending",
                "production_profile_active": False,
                "production_weights_frozen": True,
                "blockers": ["Need sealed evidence."],
                "gates": {
                    "real_agent_ablation_pairs": 9,
                    "effective_jury_model_families": 1,
                    "effective_jury_providers": 1,
                    "sealed_promotion_tasks": 0,
                    "sealed_promotion_repositories": 0,
                    "sealed_promotion_sequences": 0,
                    "promotion_approved": False,
                },
            }
        ),
        encoding="utf-8",
    )
    document = tmp_path / "README.md"
    document.write_text(
        f"# Project\n\n{READINESS_START}\nstale\n{READINESS_END}\n",
        encoding="utf-8",
    )

    assert sync_documents(readiness, [document], write=False) == [document]
    assert sync_documents(readiness, [document], write=True) == [document]
    assert sync_documents(readiness, [document], write=False) == []
    rendered = document.read_text(encoding="utf-8")
    assert "有效 real-agent 配对: `9`" in rendered
    assert "生产权重冻结: `yes`" in rendered


def test_checked_in_readiness_blocks_are_current() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (
        sync_documents(
            root / "benchmarks" / "ai_calibration_v1" / "readiness.json",
            [root / "README.md", root / "PROJECT_STATUS.md"],
            write=False,
        )
        == []
    )
