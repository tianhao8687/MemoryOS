from __future__ import annotations

import hashlib
import json
import tempfile
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from memoryos.config import settings_for
from memoryos.context.token_meter import UnicodeHeuristicTokenCounter, canonical_json
from memoryos.db import Database
from memoryos.db.models import ClaimRow
from memoryos.domain.schemas import (
    ClaimCandidate,
    ClaimModality,
    ClaimObjectKind,
    ClaimPolarity,
    ClaimStaleState,
    ClaimStatus,
    ConflictStrategy,
    ContextRequest,
    CreatedBy,
    EntityType,
    EvidenceSpan,
    MemoryCreate,
    MemoryType,
    ScopeType,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService

BASELINE_COMMIT = "d958ab77118f613dff368140ccf284f10949cfad"
FIXED_TIME = "2026-08-15T00:00:00+00:00"


def _memory(
    *,
    title: str,
    content: str,
    key: str,
    category: str,
    subject: str,
    predicate: str,
    object_value: str | int,
    modality: ClaimModality,
    polarity: ClaimPolarity = ClaimPolarity.POSITIVE,
    active: bool = True,
) -> MemoryCreate:
    return MemoryCreate(
        scope_type=ScopeType.REPOSITORY,
        scope_key="golden-repo",
        memory_type=MemoryType.PROJECT,
        category=category,
        key=key,
        title=title,
        content=content,
        created_by=CreatedBy.MANUAL if active else CreatedBy.AGENT,
        activate_immediately=active,
        source=SourceCreate(
            source_type=SourceType.MANUAL if active else SourceType.AGENT,
            source_ref=f"golden:{key}",
            excerpt=content,
        ),
        claim_candidates=[
            ClaimCandidate(
                subject_hint=subject,
                subject_type=EntityType.PROJECT,
                predicate=predicate,
                object_kind=ClaimObjectKind.LITERAL,
                object_value=object_value,
                polarity=polarity,
                modality=modality,
                evidence_span=EvidenceSpan(start=0, end=len(content), quote=content),
            )
        ],
    )


def build_fixture() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="memoryos-v22-golden-") as directory:
        settings = settings_for(Path(directory), context_compiler_mode="legacy")
        database = Database(settings)
        database.initialize()
        service = MemoryService(database, settings)
        try:
            memories: dict[str, dict[str, Any]] = {}
            memories["resolved"] = service.propose(
                _memory(
                    title="Resolved FastAPI decision",
                    content="The API framework is FastAPI.",
                    key="architecture.api.framework",
                    category="decision",
                    subject="project.api",
                    predicate="uses_framework",
                    object_value="FastAPI",
                    modality=ClaimModality.DECISION,
                ),
                actor="golden",
            )
            memories["constraint"] = service.propose(
                _memory(
                    title="Timeout safety constraint",
                    content=("Timeout must not exceed 30 seconds except offline migration jobs."),
                    key="runtime.timeout.constraint",
                    category="constraint",
                    subject="project.runtime",
                    predicate="timeout_limit_seconds",
                    object_value=30,
                    modality=ClaimModality.CONSTRAINT,
                    polarity=ClaimPolarity.NEGATIVE,
                ),
                actor="golden",
            )
            memories["contested_left"] = service.propose(
                _memory(
                    title="Contested worker FastAPI",
                    content="The worker framework is FastAPI.",
                    key="worker.framework.fastapi",
                    category="decision",
                    subject="project.worker",
                    predicate="uses_framework",
                    object_value="FastAPI",
                    modality=ClaimModality.DECISION,
                ),
                actor="golden",
            )
            right = service.propose(
                _memory(
                    title="Contested worker Django",
                    content="The worker framework is Django.",
                    key="worker.framework.django",
                    category="decision",
                    subject="project.worker",
                    predicate="uses_framework",
                    object_value="Django",
                    modality=ClaimModality.DECISION,
                    active=False,
                ),
                actor="golden",
            )
            memories["contested_right"] = service.confirm(
                right["id"],
                strategy=ConflictStrategy.KEEP_BOTH,
                rationale="Golden unresolved alternative",
                actor="golden",
            )
            memories["suspect"] = service.propose(
                _memory(
                    title="Suspect retry budget",
                    content="The worker retry budget is 3.",
                    key="worker.retry.budget",
                    category="failure",
                    subject="project.worker",
                    predicate="retry_budget",
                    object_value=3,
                    modality=ClaimModality.FAILURE,
                ),
                actor="golden",
            )
            memories["stale"] = service.propose(
                _memory(
                    title="Stale Celery queue",
                    content="The legacy queue uses Celery.",
                    key="legacy.queue.framework",
                    category="state",
                    subject="project.legacy_queue",
                    predicate="uses_framework",
                    object_value="Celery",
                    modality=ClaimModality.OBSERVATION,
                ),
                actor="golden",
            )
            memories["source_grounded"] = service.propose(
                _memory(
                    title="Source-grounded deployment state",
                    content="The deployment state is blue-green in the operator runbook.",
                    key="deployment.state",
                    category="state",
                    subject="project.deployment",
                    predicate="state",
                    object_value="blue-green",
                    modality=ClaimModality.OBSERVATION,
                ),
                actor="golden",
            )
            with database.session() as session:
                suspect_claim = session.scalar(
                    select(ClaimRow).where(ClaimRow.memory_id == memories["suspect"]["id"])
                )
                stale_claim = session.scalar(
                    select(ClaimRow).where(ClaimRow.memory_id == memories["stale"]["id"])
                )
                assert suspect_claim is not None and stale_claim is not None
                contested_claims = list(
                    session.scalars(
                        select(ClaimRow).where(
                            ClaimRow.memory_id.in_(
                                [
                                    memories["contested_left"]["id"],
                                    memories["contested_right"]["id"],
                                ]
                            )
                        )
                    )
                )
                assert len(contested_claims) == 2
                for claim in contested_claims:
                    claim.status = ClaimStatus.CONTESTED
                suspect_claim.stale_state = ClaimStaleState.SUSPECT
                stale_claim.stale_state = ClaimStaleState.STALE
                stale_claim.status = ClaimStatus.STALE
                session.execute(
                    delete(ClaimRow).where(ClaimRow.memory_id == memories["source_grounded"]["id"])
                )

            request = ContextRequest(
                task=(
                    "FastAPI Django timeout retry Celery deployment worker architecture "
                    "constraint state"
                ),
                repository="golden-repo",
                budget=50_000,
                include_historical=True,
            )
            response = service.context(request)
            retrieval_run = service.retrieval_run(response["retrieval_run_id"])
            normalized = _normalize_capture(response, retrieval_run, memories)
            return _wrap_fixture(normalized, request)
        finally:
            database.close()


def _normalize_capture(
    response: dict[str, Any],
    retrieval_run: dict[str, Any],
    memories: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    replacements = {str(memory["id"]): f"m_{name}" for name, memory in memories.items()}
    names_by_memory_id = {str(memory["id"]): name for name, memory in memories.items()}
    for item in response["manifest"]:
        name = names_by_memory_id[str(item["memory_id"])]
        for index, claim_id in enumerate(item.get("claim_ids", []), start=1):
            replacements[str(claim_id)] = f"c_{name}_{index:02d}"
    replacements[str(response["retrieval_run_id"])] = "run_v22_golden"
    normalized_response = _normalize_value(response, replacements)
    normalized_run = _normalize_value(retrieval_run, replacements)
    normalized_response["characters_used"] = len(normalized_response["text"])
    normalized_response["budget_exceeded"] = False
    normalized_run["created_at"] = FIXED_TIME
    for field in (
        "context_usage",
        "context_policy_manifest",
        "context_diagnostics",
        "context_shadow",
    ):
        normalized_run.pop(field, None)
    return {"response": normalized_response, "retrieval_run": normalized_run}


def _normalize_value(value: Any, replacements: dict[str, str], key: str = "") -> Any:
    if isinstance(value, dict):
        if key == "stage_timings_ms":
            return {item_key: 0.0 for item_key in value}
        return {
            item_key: _normalize_value(item, replacements, item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_value(item, replacements, key) for item in value]
    if isinstance(value, str):
        normalized = value
        for original, replacement in replacements.items():
            normalized = normalized.replace(original, replacement)
        if key in {"created_at", "updated_at", "recorded_at", "captured_at"}:
            return FIXED_TIME
        return normalized
    if isinstance(value, float) and ("timing" in key or "duration" in key):
        return 0.0
    return value


def _wrap_fixture(capture: dict[str, Any], request: ContextRequest) -> dict[str, Any]:
    counter = UnicodeHeuristicTokenCounter()
    response = capture["response"]
    mcp_result = {"ok": True, "result": response}

    def size(value: Any, *, text: bool = False) -> dict[str, int]:
        serialized = str(value) if text else canonical_json(value)
        return {
            "characters": len(serialized),
            "utf8_bytes": len(serialized.encode("utf-8")),
            "estimated_tokens": counter.count_text(serialized),
        }

    report_path = Path("docs/verification/v2.2/markupsafe-public-smoke/report.json")
    report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
    return {
        "schema_version": "1.0",
        "artifact": "v22_context_compiler_golden",
        "baseline_commit": BASELINE_COMMIT,
        "captured_at": FIXED_TIME,
        "normalization": (
            "UUIDs, timestamps, and timing fields are normalized; payload structure and "
            "semantic values are preserved."
        ),
        "budget_contract": {
            "field": "budget",
            "unit": "characters",
            "scope": "legacy text only",
            "token_semantics": False,
        },
        "request": {
            "task": request.task,
            "repository": request.repository,
            "branch": request.branch,
            "workspace": request.workspace,
            "task_scope": request.task_scope,
            "budget": request.budget,
            "include_historical": request.include_historical,
            "as_of_valid_time": None,
            "as_known_at": None,
        },
        "coverage_cases": [
            "resolved",
            "contested",
            "suspect",
            "stale",
            "constraint",
            "source_grounded",
        ],
        "context_response": response,
        "retrieval_run": capture["retrieval_run"],
        "canonical_mcp_tool_result": mcp_result,
        "payload_size_breakdown": {
            "text": size(response["text"], text=True),
            "sections": size(response["sections"]),
            "manifest": size(response["manifest"]),
            "debug": size(response["debug"]),
            "context_response_total": size(response),
            "mcp_tool_result_total": size(mcp_result),
            "counter_kind": counter.kind.value,
            "tokenizer_id": counter.tokenizer_id,
            "counter_version": counter.counter_version,
        },
        "real_workload_report": {
            "path": report_path.as_posix(),
            "sha256": report_hash,
            "effect_claim": "none",
            "role": "v2.2 public one-task protocol smoke; not an MSC effect claim",
        },
    }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    rendered = json.dumps(build_fixture(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(rendered, end="")
        return
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(rendered, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
