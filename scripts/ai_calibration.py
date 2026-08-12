from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from memoryos.evaluation.ai_calibration_protocol import (
    default_ai_calibration_protocol,
    load_ai_calibration_protocol,
)
from memoryos.evaluation.ai_jury import (
    PairwiseJudgeVote,
    aggregate_ai_jury,
    rank_jury_candidates,
)
from memoryos.evaluation.executable_ablation import (
    ExecutableAblationRun,
    analyze_executable_ablations,
)
from memoryos.evaluation.retrieval_weight_calibration import (
    CalibrationCandidateFeatureVector,
    LearnedWeightProfile,
    PairwiseFeatureObservation,
    WeightCandidateEvaluation,
    evaluate_weight_candidate,
    observations_from_ai_jury,
    shadow_scoring_profile_from_learned,
    train_candidate_weights,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the AI-jury, executable-ablation, and candidate-weight pipeline."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    protocol_parser = subparsers.add_parser("protocol", help="Write the default frozen protocol.")
    protocol_parser.add_argument("--output", type=Path, required=True)

    jury_parser = subparsers.add_parser("jury", help="Aggregate order-swapped AI jury votes.")
    _add_protocol_argument(jury_parser)
    jury_parser.add_argument("--votes", type=Path, required=True)
    jury_parser.add_argument("--results", type=Path, required=True)
    jury_parser.add_argument("--utilities", type=Path)
    jury_parser.add_argument("--features", type=Path)
    jury_parser.add_argument("--observations", type=Path)

    ablation_parser = subparsers.add_parser(
        "ablation", help="Analyze paired full/minus-memory executable runs."
    )
    ablation_parser.add_argument("--runs", type=Path, required=True)
    ablation_parser.add_argument("--output", type=Path, required=True)

    train_parser = subparsers.add_parser(
        "train", help="Train a non-negative regularized candidate weight profile."
    )
    _add_protocol_argument(train_parser)
    train_parser.add_argument("--observations", type=Path, required=True)
    train_parser.add_argument("--output", type=Path, required=True)
    train_parser.add_argument("--shadow-profile", type=Path)

    promote_parser = subparsers.add_parser(
        "promote", help="Evaluate a candidate profile on sealed executable outcomes."
    )
    _add_protocol_argument(promote_parser)
    promote_parser.add_argument("--profile", type=Path, required=True)
    promote_parser.add_argument("--evaluations", type=Path, required=True)
    promote_parser.add_argument("--output", type=Path, required=True)

    arguments = parser.parse_args()
    summary: dict[str, object]
    if arguments.command == "protocol":
        protocol = default_ai_calibration_protocol()
        _write_model(arguments.output, protocol)
        summary = {"status": protocol.status, "protocol_sha256": protocol.digest()}
    elif arguments.command == "jury":
        protocol = load_ai_calibration_protocol(arguments.protocol)
        votes = _load_jsonl(arguments.votes, PairwiseJudgeVote)
        results = aggregate_ai_jury(votes, protocol=protocol.ai_jury)
        _write_models(arguments.results, results)
        utilities = rank_jury_candidates(results) if arguments.utilities is not None else []
        if arguments.utilities is not None:
            _write_models(arguments.utilities, utilities)
        if (arguments.features is None) != (arguments.observations is None):
            raise ValueError("jury --features and --observations must be supplied together")
        observations = []
        if arguments.features is not None:
            features = _load_jsonl(arguments.features, CalibrationCandidateFeatureVector)
            observations = observations_from_ai_jury(results, features)
            _write_models(arguments.observations, observations)
        summary = {
            "status": "ai_jury_aggregated",
            "votes": len(votes),
            "comparisons": len(results),
            "utilities": len(utilities),
            "observations": len(observations),
            "production_eligible": False,
        }
    elif arguments.command == "ablation":
        runs = _load_jsonl(arguments.runs, ExecutableAblationRun)
        report = analyze_executable_ablations(runs)
        _write_model(arguments.output, report)
        summary = {
            "status": report.status,
            "runs": report.total_runs,
            "effects": len(report.effects),
            "real_agent_effects": report.real_agent_effects,
            "production_eligible": report.production_eligible,
        }
    elif arguments.command == "train":
        protocol = load_ai_calibration_protocol(arguments.protocol)
        observations = _load_jsonl(arguments.observations, PairwiseFeatureObservation)
        profile = train_candidate_weights(observations, protocol.weight_training)
        _write_model(arguments.output, profile)
        shadow_profile = shadow_scoring_profile_from_learned(profile)
        if arguments.shadow_profile is not None:
            _write_model(arguments.shadow_profile, shadow_profile)
        summary = {
            "status": profile.status,
            "observations": len(observations),
            "observations_sha256": profile.observations_sha256,
            "profile_sha256": profile.profile_sha256,
            "shadow_profile_sha256": shadow_profile.digest(),
            "production_eligible": profile.production_eligible,
        }
    else:
        protocol = load_ai_calibration_protocol(arguments.protocol)
        profile = _load_json_model(arguments.profile, LearnedWeightProfile)
        evaluations = _load_jsonl(arguments.evaluations, WeightCandidateEvaluation)
        decision = evaluate_weight_candidate(
            profile,
            evaluations,
            protocol=protocol.promotion,
        )
        _write_model(arguments.output, decision)
        summary = {
            "status": decision.status,
            "approved": decision.approved,
            "tasks": decision.tasks,
            "activates_automatically": decision.activates_automatically,
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def _add_protocol_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--protocol",
        type=Path,
        default=Path("benchmarks/ai_calibration_v1/protocol.json"),
    )


def _load_jsonl[ModelT: BaseModel](path: Path, model: type[ModelT]) -> list[ModelT]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError(f"JSONL artifact is not UTF-8: {path}") from exc
    if any(not line.strip() for line in lines):
        raise ValueError(f"JSONL artifact contains blank rows: {path}")
    adapter = TypeAdapter(model)
    values: list[ModelT] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            values.append(adapter.validate_python(json.loads(line)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSONL row {path}:{line_number}") from exc
    return values


def _load_json_model[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        payload = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 JSON model: {path}") from exc
    return TypeAdapter(model).validate_python(payload)


def _write_models(path: Path, values: Sequence[BaseModel]) -> None:
    encoded = "".join(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
        for value in values
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8", newline="\n")


def _write_model(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
