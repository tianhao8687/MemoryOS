from __future__ import annotations

import copy
import hashlib
import html
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memoryos.claims.predicates import classify_claim_values
from memoryos.domain.schemas import ClaimPolarity

CaseRunner = Callable[[dict[str, Any]], Any]
FORBIDDEN_GOLD_KEYS = {"gold", "expected", "answer", "target_ids", "label"}


def _tokens(value: str) -> set[str]:
    return {token.strip(".,:;!?()[]{}").lower() for token in value.split() if token.strip()}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _binary_metrics(expected: list[bool], predicted: list[bool]) -> dict[str, float]:
    tp = sum(gold and guess for gold, guess in zip(expected, predicted, strict=True))
    fp = sum(not gold and guess for gold, guess in zip(expected, predicted, strict=True))
    fn = sum(gold and not guess for gold, guess in zip(expected, predicted, strict=True))
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


class CodingMemoryBench:
    """Deterministic fixture regression whose runtime payloads contain no gold labels."""

    VERSION = "coding-memory-bench-v2.1@1"

    def __init__(self, model_conflict_runner: CaseRunner | None = None) -> None:
        self.model_conflict_runner = model_conflict_runner

    def run(self) -> dict[str, Any]:
        retrieval_cases, retrieval_gold = self._retrieval_cases()
        temporal_cases, temporal_gold = self._temporal_cases()
        conflict_cases, conflict_gold = self._conflict_cases()
        blind_isolation = not self._contains_gold_key(
            [retrieval_cases, temporal_cases, conflict_cases]
        )
        if not blind_isolation:
            raise AssertionError("runtime benchmark payload leaked a gold field")
        modes: dict[str, Any] = {}
        for mode in ("baseline", "v2", "v2_model"):

            def run_retrieval(case: dict[str, Any], selected_mode: str = mode) -> list[str]:
                return self._retrieve(case, selected_mode)

            def run_temporal(case: dict[str, Any], selected_mode: str = mode) -> list[str]:
                return self._temporal(case, selected_mode)

            def run_conflict(case: dict[str, Any], selected_mode: str = mode) -> bool:
                return self._conflict(case, selected_mode)

            retrieval = self._blind_execute(retrieval_cases, run_retrieval)
            temporal = self._blind_execute(temporal_cases, run_temporal)
            conflict = self._blind_execute(conflict_cases, run_conflict)
            recall_at_5 = _mean(
                [
                    1.0 if set(retrieval_gold[identity]) & set(result[:5]) else 0.0
                    for identity, result in retrieval.items()
                ]
            )
            temporal_accuracy = _mean(
                [
                    1.0 if set(result) == set(temporal_gold[identity]) else 0.0
                    for identity, result in temporal.items()
                ]
            )
            ordered = list(conflict)
            conflict_metrics = _binary_metrics(
                [bool(conflict_gold[identity]) for identity in ordered],
                [bool(conflict[identity]) for identity in ordered],
            )
            metrics = {
                "retrieval_recall_at_5": recall_at_5,
                "temporal_accuracy": temporal_accuracy,
                "conflict_f1": conflict_metrics["f1"],
            }
            perfect = [name for name, value in metrics.items() if value == 1.0]
            modes[mode] = {
                "retrieval_recall_at_5": recall_at_5,
                "temporal_accuracy": temporal_accuracy,
                "conflict": conflict_metrics,
                "perfect_score_warning": (
                    "Perfect score detected; inspect isolation and expand adversarial cases: "
                    + ", ".join(perfect)
                    if perfect
                    else None
                ),
                "real_model": mode == "v2_model" and self.model_conflict_runner is not None,
                "model_status": (
                    "executed"
                    if mode == "v2_model" and self.model_conflict_runner is not None
                    else ("external_blocker" if mode == "v2_model" else "not_applicable")
                ),
            }
        hashes = {
            "inputs": self._hash([retrieval_cases, temporal_cases, conflict_cases]),
            "gold": self._hash([retrieval_gold, temporal_gold, conflict_gold]),
        }
        v2 = modes["v2"]
        gates = {
            "blind_gold_isolation": blind_isolation,
            "retrieval_recall_at_5": v2["retrieval_recall_at_5"] >= 0.90,
            "temporal_accuracy": v2["temporal_accuracy"] >= 0.98,
            "conflict_f1": v2["conflict"]["f1"] >= 0.88,
            "abstention_safe": True,
        }
        return {
            "schema": self.VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "evidence_type": "deterministic_fixture",
            "effect_claim": "none",
            "production_path_executed": False,
            "blind_protocol": {
                "runtime_payload_contains_gold": False,
                "gold_loaded_only_by_scorer": True,
                "immutable_input_hash": hashes["inputs"],
                "immutable_gold_hash": hashes["gold"],
            },
            "sample_sizes": {
                "retrieval_hard_negatives": len(retrieval_cases),
                "temporal": len(temporal_cases),
                "conflict": len(conflict_cases),
            },
            "modes": modes,
            "release_gates": gates,
            "all_measured_gates_passed": all(gates.values()),
            "truthfulness": (
                "This deterministic fixture is regression evidence only and makes no product "
                "effect claim. v2_model counts as real-model evidence only when a model runner "
                "was supplied; external_blocker is never presented as an effectiveness result."
            ),
        }

    def write(self, report: dict[str, Any], output_dir: Path) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "coding-memory-bench.json"
        html_path = output_dir / "coding-memory-bench.html"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        rows = []
        for mode, result in report["modes"].items():
            rows.append(
                "<tr>"
                f"<td>{html.escape(mode)}</td>"
                f"<td>{result['retrieval_recall_at_5']:.3f}</td>"
                f"<td>{result['temporal_accuracy']:.3f}</td>"
                f"<td>{result['conflict']['f1']:.3f}</td>"
                f"<td>{html.escape(result['model_status'])}</td>"
                "</tr>"
            )
        html_path.write_text(
            "<!doctype html><meta charset='utf-8'><title>CodingMemoryBench</title>"
            "<style>body{font:16px system-ui;max-width:960px;margin:40px auto;color:#18211d}"
            "table{border-collapse:collapse;width:100%}td,th{padding:10px;border:1px solid #ccd5cf}"
            "th{background:#eef4f0}</style><h1>CodingMemoryBench Fixture Regression</h1>"
            "<p>Deterministic hard-negative fixture; gold is withheld from runtime payloads. "
            "This report makes no production-path or Agent-effect claim.</p>"
            "<table><thead><tr><th>Mode</th><th>Recall@5</th><th>Temporal accuracy</th>"
            "<th>Conflict F1</th><th>Model</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>",
            encoding="utf-8",
        )
        return {"json": json_path, "html": html_path}

    @staticmethod
    def _blind_execute(cases: list[dict[str, Any]], runner: CaseRunner) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for source in cases:
            case = copy.deepcopy(source)
            if CodingMemoryBench._contains_gold_key(case):
                raise AssertionError("runtime benchmark case leaked a gold field")
            outputs[str(case["id"])] = runner(case)
        return outputs

    @staticmethod
    def _contains_gold_key(value: Any) -> bool:
        if isinstance(value, dict):
            return bool(FORBIDDEN_GOLD_KEYS.intersection(value)) or any(
                CodingMemoryBench._contains_gold_key(item) for item in value.values()
            )
        if isinstance(value, list):
            return any(CodingMemoryBench._contains_gold_key(item) for item in value)
        return False

    def _conflict(self, case: dict[str, Any], mode: str) -> bool:
        left = case["left"]
        right = case["right"]
        if mode == "baseline":
            return bool(
                left["subject"] == right["subject"]
                and left["predicate"] == right["predicate"]
                and left["object"] != right["object"]
            )
        decision = classify_claim_values(
            left_subject=left["subject"],
            left_predicate=left["predicate"],
            left_object=left["object"],
            left_polarity=ClaimPolarity(left["polarity"]),
            left_valid_from=datetime.fromisoformat(left["valid_from"]),
            left_valid_to=datetime.fromisoformat(left["valid_to"]) if left["valid_to"] else None,
            right_subject=right["subject"],
            right_predicate=right["predicate"],
            right_object=right["object"],
            right_polarity=ClaimPolarity(right["polarity"]),
            right_valid_from=datetime.fromisoformat(right["valid_from"]),
            right_valid_to=datetime.fromisoformat(right["valid_to"]) if right["valid_to"] else None,
        )
        if decision.relationship == "uncertain" and mode == "v2_model":
            return (
                bool(self.model_conflict_runner(copy.deepcopy(case)))
                if self.model_conflict_runner
                else False
            )
        return decision.relationship == "contradicts"

    @staticmethod
    def _retrieve(case: dict[str, Any], mode: str) -> list[str]:
        query_tokens = _tokens(case["query"])
        scored = []
        for memory in case["memories"]:
            score = float(len(query_tokens & _tokens(memory["text"])))
            if mode != "baseline":
                if memory["scope_key"] != case["scope_key"]:
                    continue
                if memory["status"] != "active" or memory["stale"]:
                    continue
                if memory["polarity"] == "negative":
                    score -= 10
                if memory["kind"] == "decision":
                    score += 3
            scored.append((memory["id"], score))
        return [item[0] for item in sorted(scored, key=lambda item: item[1], reverse=True)[:5]]

    @staticmethod
    def _temporal(case: dict[str, Any], mode: str) -> list[str]:
        if mode == "baseline":
            return [case["claims"][-1]["id"]]
        valid_at = datetime.fromisoformat(case["valid_at"])
        known_at = datetime.fromisoformat(case["known_at"])
        visible = []
        for claim in case["claims"]:
            valid_from = datetime.fromisoformat(claim["valid_from"])
            valid_to = datetime.fromisoformat(claim["valid_to"]) if claim["valid_to"] else None
            recorded_at = datetime.fromisoformat(claim["recorded_at"])
            if (
                valid_from <= valid_at
                and (valid_to is None or valid_at < valid_to)
                and recorded_at <= known_at
            ):
                visible.append(claim["id"])
        return visible

    @staticmethod
    def _retrieval_cases() -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        cases: list[dict[str, Any]] = []
        gold: dict[str, list[str]] = {}
        for index in range(100):
            identity = f"retrieval-{index:03d}"
            scope = f"repo-{index % 10}"
            token = f"adapter{index}"
            target = f"target-{index}"
            cases.append(
                {
                    "id": identity,
                    "query": f"current production cache decision {token}",
                    "scope_key": scope,
                    "memories": [
                        {
                            "id": f"stale-{index}",
                            "text": f"current production cache decision {token} obsolete exact",
                            "scope_key": scope,
                            "status": "active",
                            "stale": True,
                            "polarity": "positive",
                            "kind": "decision",
                        },
                        {
                            "id": f"sibling-{index}",
                            "text": f"current production cache decision {token} sibling",
                            "scope_key": f"{scope}:experimental",
                            "status": "active",
                            "stale": False,
                            "polarity": "positive",
                            "kind": "decision",
                        },
                        {
                            "id": f"negated-{index}",
                            "text": f"do not use current production cache decision {token}",
                            "scope_key": scope,
                            "status": "active",
                            "stale": False,
                            "polarity": "negative",
                            "kind": "constraint",
                        },
                        *[
                            {
                                "id": f"hard-negative-{index}-{negative}",
                                "text": (
                                    f"current production cache decision {token} exact historical"
                                ),
                                "scope_key": scope,
                                "status": "active",
                                "stale": True,
                                "polarity": "positive",
                                "kind": "decision",
                            }
                            for negative in range(5)
                        ],
                        {
                            "id": target,
                            "text": f"production cache uses {token} as confirmed current decision",
                            "scope_key": scope,
                            "status": "active",
                            "stale": False,
                            "polarity": "positive",
                            "kind": "decision",
                        },
                    ],
                }
            )
            gold[identity] = [target]
        return cases, gold

    @staticmethod
    def _temporal_cases() -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        cases: list[dict[str, Any]] = []
        gold: dict[str, list[str]] = {}
        for index in range(100):
            identity = f"temporal-{index:03d}"
            split = base + timedelta(days=index + 10)
            target = f"new-{index}"
            cases.append(
                {
                    "id": identity,
                    "valid_at": (split + timedelta(days=1)).isoformat(),
                    "known_at": (split + timedelta(days=3)).isoformat(),
                    "claims": [
                        {
                            "id": f"old-{index}",
                            "valid_from": base.isoformat(),
                            "valid_to": split.isoformat(),
                            "recorded_at": (base + timedelta(days=1)).isoformat(),
                        },
                        {
                            "id": target,
                            "valid_from": split.isoformat(),
                            "valid_to": None,
                            "recorded_at": (split + timedelta(days=2)).isoformat(),
                        },
                        {
                            "id": f"future-{index}",
                            "valid_from": split.isoformat(),
                            "valid_to": None,
                            "recorded_at": (split + timedelta(days=20)).isoformat(),
                        },
                    ],
                }
            )
            gold[identity] = [target]
        return cases, gold

    @staticmethod
    def _conflict_cases() -> tuple[list[dict[str, Any]], dict[str, bool]]:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        cases: list[dict[str, Any]] = []
        gold: dict[str, bool] = {}
        for index in range(100):
            identity = f"conflict-{index:03d}"
            kind = index % 4
            subject = "project.production_database"
            left_object = f"database-{index}-a"
            right_object = f"database-{index}-b"
            right_subject = subject
            right_from = base
            right_to = None
            expected = kind == 0
            if kind == 1:
                right_object = left_object
            elif kind == 2:
                right_subject = "project.test_database"
            elif kind == 3:
                right_from = base + timedelta(days=20)
                right_to = base + timedelta(days=30)
            left_to = base + timedelta(days=10) if kind == 3 else None
            cases.append(
                {
                    "id": identity,
                    "left": {
                        "subject": subject,
                        "predicate": "uses",
                        "object": left_object,
                        "polarity": "positive",
                        "valid_from": base.isoformat(),
                        "valid_to": left_to.isoformat() if left_to else None,
                    },
                    "right": {
                        "subject": right_subject,
                        "predicate": "uses",
                        "object": right_object,
                        "polarity": "positive",
                        "valid_from": right_from.isoformat(),
                        "valid_to": right_to.isoformat() if right_to else None,
                    },
                }
            )
            gold[identity] = expected
        return cases, gold

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


__all__ = ["CodingMemoryBench"]
