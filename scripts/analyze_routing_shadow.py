from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.retrieval_routing_evaluation import (
    RoutingCandidateEvaluation,
    RoutingPromotionProtocol,
    evaluate_routing_candidate,
)
from memoryos.retrieval_v2.routing import load_routing_shadow_profile


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate routing-shadow task pairs with repository and recipe safety gates."
    )
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--evaluations", type=Path, nargs="+", required=True)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--bootstrap-seed", type=int, default=20260813)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    output = arguments.output.resolve()
    if output.exists():
        raise ValueError(f"refusing to overwrite routing analysis: {output}")
    profile = load_routing_shadow_profile(arguments.profile)
    protocol = (
        RoutingPromotionProtocol()
        if arguments.protocol is None
        else _load_protocol(arguments.protocol)
    )
    evaluations = [
        evaluation for path in arguments.evaluations for evaluation in _load_evaluations(path)
    ]
    decision = evaluate_routing_candidate(
        profile,
        evaluations,
        protocol=protocol,
        bootstrap_seed=arguments.bootstrap_seed,
    )
    payload = {
        "status": "routing_shadow_analysis_complete",
        "profile": profile.model_dump(mode="json"),
        "protocol": protocol.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "evaluation_count": len(evaluations),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _load_evaluations(path: Path) -> list[RoutingCandidateEvaluation]:
    resolved = path.resolve(strict=True)
    result: list[RoutingCandidateEvaluation] = []
    for line_number, raw_line in enumerate(
        resolved.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
            result.append(RoutingCandidateEvaluation.model_validate(payload))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid routing evaluation at {resolved}:{line_number}") from exc
    if not result:
        raise ValueError(f"routing evaluation file is empty: {resolved}")
    return result


def _load_protocol(path: Path) -> RoutingPromotionProtocol:
    resolved = path.resolve(strict=True)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid routing promotion protocol: {resolved}") from exc
    return RoutingPromotionProtocol.model_validate(payload)


if __name__ == "__main__":
    main()
