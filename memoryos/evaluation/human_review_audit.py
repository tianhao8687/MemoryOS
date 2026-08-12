from __future__ import annotations

import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from memoryos.evaluation.human_review_models import ReviewSourceKind, ReviewSourceSnapshot


class CouplingRisk(StrEnum):
    BLOCKING = "blocking"
    MATERIAL = "material"
    CONTROLLED = "controlled"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CouplingFinding(StrictModel):
    risk: CouplingRisk
    code: str = Field(min_length=1, max_length=120)
    detail: str = Field(min_length=1, max_length=2000)


class HoldoutFoldPlan(StrictModel):
    id: str = Field(min_length=1, max_length=160)
    held_out_source_id: str = Field(min_length=1, max_length=160)
    training_source_ids: list[str]
    repository_overlap: list[str]
    status: Literal["pending_human_labels"] = "pending_human_labels"
    interpretation: str = Field(min_length=1, max_length=1000)


class CouplingAuditReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    dataset_id: str = Field(min_length=1, max_length=160)
    status: Literal["pilot_only"] = "pilot_only"
    label_state: Literal["pending_human_adjudication"] = "pending_human_adjudication"
    total_cases: int = Field(ge=1)
    cases_by_source_kind: dict[str, int]
    source_dataset_ids: list[str]
    git_repository_ids: list[str]
    real_workload_repository_ids: list[str]
    external_real_workload_repository_ids: list[str]
    overlapping_real_workload_repository_ids: list[str]
    protections: dict[str, bool]
    findings: list[CouplingFinding] = Field(min_length=1)
    leave_one_source_out_plan: list[HoldoutFoldPlan] = Field(min_length=1)
    release_blockers: list[str] = Field(min_length=1)
    next_data_requirements: list[str] = Field(min_length=1)

    def canonical_json(self) -> str:
        return (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


def build_coupling_audit(
    *,
    dataset_id: str,
    sources: list[ReviewSourceSnapshot],
    cases_by_source_kind: dict[str, int],
    total_cases: int,
) -> CouplingAuditReport:
    git_repositories = sorted(
        {
            repository
            for source in sources
            if source.kind is ReviewSourceKind.GIT_HISTORY
            for repository in source.repositories
        }
    )
    real_repositories = sorted(
        {
            repository
            for source in sources
            if source.kind is ReviewSourceKind.REAL_WORKLOAD
            for repository in source.repositories
        }
    )
    external_real = sorted(set(real_repositories) - set(git_repositories))
    overlapping_real = sorted(set(real_repositories) & set(git_repositories))
    largest_kind, largest_count = max(
        cases_by_source_kind.items(), key=lambda item: (item[1], item[0])
    )

    findings = [
        CouplingFinding(
            risk=CouplingRisk.CONTROLLED,
            code="sealed-test-not-sampled",
            detail=(
                "The existing repository-held-out test split is absent from the review pack and "
                "remains sealed for confirmatory evaluation."
            ),
        ),
        CouplingFinding(
            risk=CouplingRisk.MATERIAL,
            code="source-kind-concentration",
            detail=(
                f"The largest source kind ({largest_kind}) contributes {largest_count} of "
                f"{total_cases} cases. Report it as a raw concentration; do not hide it in an "
                "aggregate score."
            ),
        ),
        CouplingFinding(
            risk=CouplingRisk.BLOCKING,
            code="human-labels-pending",
            detail=(
                "The pack contains blank independent-review templates, not human judgments. It "
                "cannot be called gold or used to approve production weights."
            ),
        ),
    ]
    if overlapping_real:
        findings.append(
            CouplingFinding(
                risk=CouplingRisk.MATERIAL,
                code="real-workload-repository-overlap",
                detail=(
                    "Real-workload repositories overlap Git-history sources: "
                    f"{', '.join(overlapping_real)}. These tasks are diagnostic, not independent "
                    "external validation."
                ),
            )
        )
    if not external_real:
        findings.append(
            CouplingFinding(
                risk=CouplingRisk.BLOCKING,
                code="no-external-real-workload-repository",
                detail=(
                    "No real-workload task comes from a repository outside the Git-history source "
                    "set, so cross-source generalization is still unmeasured."
                ),
            )
        )

    folds: list[HoldoutFoldPlan] = []
    for source in sources:
        training = [candidate for candidate in sources if candidate.id != source.id]
        training_repositories = {
            repository for candidate in training for repository in candidate.repositories
        }
        overlap = sorted(set(source.repositories) & training_repositories)
        interpretation = (
            "Score this source only after tuning on the other listed sources. Repository overlap "
            "must be reported beside the fold result."
        )
        if not training:
            interpretation = "No independent training source exists; this fold is not executable."
        folds.append(
            HoldoutFoldPlan(
                id=f"holdout-{source.id}",
                held_out_source_id=source.id,
                training_source_ids=sorted(item.id for item in training),
                repository_overlap=overlap,
                interpretation=interpretation,
            )
        )

    return CouplingAuditReport(
        dataset_id=dataset_id,
        total_cases=total_cases,
        cases_by_source_kind=dict(sorted(cases_by_source_kind.items())),
        source_dataset_ids=sorted(source.id for source in sources),
        git_repository_ids=git_repositories,
        real_workload_repository_ids=real_repositories,
        external_real_workload_repository_ids=external_real,
        overlapping_real_workload_repository_ids=overlapping_real,
        protections={
            "candidate_order_varies_by_reviewer": True,
            "qrels_not_loaded_by_builder": True,
            "repository_and_time_stratified_sampling": True,
            "test_split_sealed": True,
            "two_independent_reviews_required": True,
            "independent_adjudicator_required": True,
        },
        findings=findings,
        leave_one_source_out_plan=folds,
        release_blockers=[
            "Two independent completed human reviews are missing.",
            "Independent adjudication is missing.",
            "A sealed human-gold test from repositories absent from silver calibration is missing.",
            "Real coding-agent shadow outcomes are not yet linked to adjudicated retrieval labels.",
        ],
        next_data_requirements=[
            "Add public or consented real tasks from repositories and organizations absent from "
            "memoryos-git-silver-v1.",
            "Keep each new task source as a named evaluation slice; do not pool away its identity.",
            "After adjudication, execute every leave-one-source-out fold and publish worst-slice "
            "results beside aggregate metrics.",
            "Keep truth-conflict, health, and automatic-mutation calibration in separate datasets.",
        ],
    )


__all__ = [
    "CouplingAuditReport",
    "CouplingFinding",
    "CouplingRisk",
    "HoldoutFoldPlan",
    "build_coupling_audit",
]
