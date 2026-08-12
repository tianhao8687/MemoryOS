from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from memoryos.evaluation.ai_calibration_protocol import (
    _file_sha256,
    default_ai_calibration_protocol,
    load_ai_calibration_protocol,
    validate_ai_calibration_assets,
)


def test_default_ai_only_protocol_is_frozen_and_reproducible() -> None:
    protocol = default_ai_calibration_protocol()

    assert protocol.human_review_required is False
    assert protocol.human_gold_claim is False
    assert protocol.production_weights_frozen is True
    assert protocol.activation_requires_promotion_decision is True
    assert protocol.ai_jury.min_model_families == 3
    assert protocol.ai_jury.min_providers == 3
    assert protocol.ai_jury.threshold_provenance == "provisional_policy_default"
    assert protocol.promotion.min_agent_models == 2
    assert protocol.promotion.require_complete_cost_accounting is True
    assert protocol.promotion.expected_training_protocol_sha256 == (
        "9e80ddb689d7ea919a915eb6c6258c0d4b30fcafe4f1760d53a7c98288efc796"
    )
    assert set(protocol.weight_training.feature_names).isdisjoint(
        protocol.weight_training.hard_gate_features
    )
    assert protocol.digest() == ("9114422c4305da73cad730f120434fabf9bf234a68a1ae115bd5ce7facab75d2")


def test_protocol_round_trip_and_strict_schema(tmp_path: Path) -> None:
    protocol = default_ai_calibration_protocol()
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(
        json.dumps(protocol.model_dump(mode="json")),
        encoding="utf-8",
    )

    assert load_ai_calibration_protocol(protocol_path) == protocol

    payload = protocol.model_dump(mode="json")
    payload["silent_auto_activation"] = True
    protocol_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_ai_calibration_protocol(protocol_path)


def test_evidence_hash_is_stable_across_text_checkout_line_endings(tmp_path: Path) -> None:
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(b'{\n  "ok": true\n}\n')
    crlf.write_bytes(b'{\r\n  "ok": true\r\n}\r\n')

    assert _file_sha256(lf) == _file_sha256(crlf)


def test_checked_in_ai_calibration_evidence_inventory_is_consistent() -> None:
    root = Path(__file__).resolve().parents[1]

    readiness = validate_ai_calibration_assets(root)

    assert readiness.status == "protocol_ready_evidence_pending"
    assert readiness.gates.effective_jury_model_families == 1
    assert readiness.gates.effective_jury_providers == 1
    assert readiness.gates.real_agent_ablation_pairs == 2
    assert readiness.gates.candidate_profile_available is False
    assert readiness.gates.promotion_approved is False
    assert readiness.production_profile_active is False
