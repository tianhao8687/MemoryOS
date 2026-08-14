from __future__ import annotations

import hashlib
import logging
import math
import time
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from memoryos.config import MemoryOSSettings
from memoryos.context.atoms import AtomBuilder, ContextAtom, exact_deduplicate
from memoryos.context.budget import BudgetDecision, BudgetPlanner, build_bundles
from memoryos.context.delta import ContextSnapshotStore, plan_delta
from memoryos.context.renderers import (
    MSC_SCHEMA_VERSION,
    PAYLOAD_ACCOUNTING_MAX_ROUNDS,
    aggregate_truth_state,
    make_usage,
    render_delta,
    render_full,
    stabilize_payload_usage,
)
from memoryos.context.token_meter import (
    TokenCounter,
    UnicodeHeuristicTokenCounter,
    canonical_json,
    counter_fingerprint,
)
from memoryos.db.models import (
    ClaimEvidenceRow,
    ClaimRow,
    EntityRow,
    MemoryRow,
    MemorySourceRow,
    RetrievalRunRow,
    SourceAnchorRow,
    SourceRow,
)
from memoryos.domain.schemas import (
    ContextRequest,
    MemoryOperationTokenAttribution,
    MSCContextResponse,
    QueryIntent,
    SearchRequest,
)
from memoryos.errors import MemoryOSError, TokenizerUnavailableError
from memoryos.health import MemoryHealthService
from memoryos.retrieval.context import SECTION_ORDER, _section
from memoryos.retrieval_v2 import RetrievalPipeline
from memoryos.retrieval_v2.stages import LEGACY_SCORE_CONTRACT, NORMALIZED_SCORE_CONTRACT

COVERAGE = {
    QueryIntent.CURRENT_DECISION: ["decision", "constraint", "failure"],
    QueryIntent.CONSTRAINT_LOOKUP: ["constraint", "decision"],
    QueryIntent.FAILURE_HISTORY: ["failure", "decision", "constraint"],
    QueryIntent.WHY_DECISION: ["decision", "failure"],
    QueryIntent.IMPLEMENTATION_LOCATION: ["implementation", "decision"],
    QueryIntent.PREFERENCE: ["preference"],
    QueryIntent.TASK_STATE: ["state", "decision", "constraint"],
    QueryIntent.HISTORICAL_AS_OF: ["decision", "state"],
    QueryIntent.BROAD_SEARCH: [],
}

logger = logging.getLogger(__name__)


class TaskAwareContextCompiler:
    def __init__(
        self,
        retrieval: RetrievalPipeline,
        settings: MemoryOSSettings | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.retrieval = retrieval
        self.settings = settings or retrieval.database.settings
        self.token_counter = token_counter or UnicodeHeuristicTokenCounter()
        self.snapshot_store = ContextSnapshotStore(retrieval.database, self.settings)

    def build(self, request: ContextRequest) -> dict[str, Any]:
        legacy = self._build_legacy(request)
        if self.settings.context_compiler_mode == "legacy":
            return legacy
        try:
            compact = self._build_msc(request, legacy)
        except Exception as exc:
            if self.settings.context_compiler_mode == "msc":
                raise
            self._save_shadow_error(str(legacy["retrieval_run_id"]), exc)
            return legacy
        if self.settings.context_compiler_mode == "msc_shadow":
            return legacy
        return compact

    def _build_legacy(self, request: ContextRequest) -> dict[str, Any]:
        allowed: set[tuple[str, str | None]] = {
            ("user", None),
            ("repository", request.repository),
        }
        if request.workspace:
            allowed.add(("workspace", request.workspace))
        if request.branch:
            allowed.add(("branch", f"{request.repository}:{request.branch}"))
            allowed.add(("branch", request.branch))
        if request.task_scope:
            allowed.add(("task", request.task_scope))
        result = self.retrieval.search(
            SearchRequest(
                query=request.task,
                include_history=request.include_historical,
                as_of_valid_time=request.as_of_valid_time,
                as_known_at=request.as_known_at,
                limit=80,
            ),
            allowed_scopes=allowed,
            task=request.task,
            repository=request.repository,
            branch=request.branch,
            workspace=request.workspace,
            task_scope=request.task_scope,
            record_retrieval=False,
        )
        self._validate_score_contract(result)
        candidates = list(result["items"])
        intent = QueryIntent(result["query_plan"]["intent"])
        shadow_profile_active = result.get("scoring_profile_sha256") is not None
        metadata = self._metadata(candidates)
        manifest = []
        prepared = []
        for item in candidates:
            memory = item["memory"]
            identity = str(memory["id"])
            freshness = str(item["trace"]["freshness"])
            confidence = float(memory.get("confidence", 0.5))
            evidence_factor = 1.0 + min(int(item["trace"]["evidence_count"]), 3) * 0.05
            freshness_factor = {"fresh": 1.0, "unknown": 0.82, "suspect": 0.3, "stale": 0.0}[
                freshness
            ]
            utility = max(float(item["score"]), 0.000001)
            if not shadow_profile_active:
                utility *= confidence * evidence_factor * freshness_factor
            prefix = "CONTESTED: " if item["truth_state"] == "contested" else ""
            if freshness == "suspect":
                prefix += "SUSPECT: "
            source_ref = metadata[identity]["source_ref"]
            line = (
                f"- [{identity}] {prefix}{memory['title']}: {memory['content']} "
                f"(source: {source_ref})"
            )
            section = _section(memory)
            prepared.append(
                {
                    "item": item,
                    "memory_id": identity,
                    "utility": utility,
                    # Conservatively charge the section heading for every item.
                    # This slightly under-fills a budget when several items share a
                    # section, but prevents the rendered text from silently exceeding it.
                    "cost": len(line) + len(section) + 3,
                    "line": line,
                    "section": section,
                    "category": str(memory["category"]).lower(),
                    "claim_groups": metadata[identity]["claim_groups"],
                    "source_ref": source_ref,
                }
            )
        prepared.sort(
            key=lambda value: float(value["utility"]) / max(1, int(value["cost"])),
            reverse=True,
        )
        contested_by_group: dict[str, set[str]] = defaultdict(set)
        groups_by_memory: dict[str, set[str]] = defaultdict(set)
        contested_candidates: dict[str, dict[str, Any]] = {}
        for candidate in prepared:
            if candidate["item"]["truth_state"] != "contested":
                continue
            identity = str(candidate["memory_id"])
            contested_candidates[identity] = candidate
            claim_groups = candidate["claim_groups"] or [f"memory:{identity}"]
            for group in claim_groups:
                contested_by_group[group].add(identity)
                groups_by_memory[identity].add(group)
        contested_components: dict[str, list[dict[str, Any]]] = {}
        visited: set[str] = set()
        for identity in contested_candidates:
            if identity in visited:
                continue
            pending = [identity]
            component_ids: set[str] = set()
            while pending:
                current = pending.pop()
                if current in component_ids:
                    continue
                component_ids.add(current)
                for group in groups_by_memory[current]:
                    pending.extend(contested_by_group[group] - component_ids)
            visited.update(component_ids)
            component = [
                candidate for candidate in prepared if str(candidate["memory_id"]) in component_ids
            ]
            for component_id in component_ids:
                contested_components[component_id] = component
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()
        used = len("Project Memory Context\n")

        def include(candidate: dict[str, Any], reason: str) -> bool:
            nonlocal used
            identity = str(candidate["memory_id"])
            if identity in selected_ids:
                return True
            bundle = contested_components.get(identity, [candidate])
            additions = [
                bundled for bundled in bundle if str(bundled["memory_id"]) not in selected_ids
            ]
            cost = sum(int(bundled["cost"]) for bundled in additions)
            if used + cost > request.budget:
                return False
            for bundled in additions:
                bundled_id = str(bundled["memory_id"])
                inclusion_reason = (
                    reason
                    if bundled_id == identity
                    else f"contested group completeness: {identity}"
                )
                selected.append({**bundled, "inclusion_reason": inclusion_reason})
                selected_ids.add(bundled_id)
            used += cost
            return True

        for category in COVERAGE[intent]:
            match = next((item for item in prepared if item["category"] == category), None)
            if match is not None:
                include(match, f"required coverage: {category}")
        for candidate in prepared:
            include(candidate, "highest utility per context cost")

        sections: dict[str, list[dict[str, Any]]] = {name: [] for name in SECTION_ORDER}
        for selected_item in selected:
            item = selected_item["item"]
            memory = item["memory"]
            section = _section(memory)
            sections[section].append(
                {
                    **memory,
                    "score": item["score"],
                    "provenance_ref": selected_item["source_ref"],
                    "truth_state": item["truth_state"],
                    "freshness": item["trace"]["freshness"],
                    "retrieval_trace": item["trace"],
                }
            )
        for candidate in prepared:
            identity = str(candidate["memory_id"])
            included = identity in selected_ids
            manifest.append(
                {
                    "memory_id": identity,
                    "claim_ids": candidate["item"]["claim_ids"],
                    "included": included,
                    "inclusion_reason": next(
                        (
                            item["inclusion_reason"]
                            for item in selected
                            if item["memory_id"] == identity
                        ),
                        None,
                    ),
                    "exclusion_reason": None if included else "budget or lower utility",
                    "utility": round(float(candidate["utility"]), 8),
                    "cost": candidate["cost"],
                    "truth_state": candidate["item"]["truth_state"],
                    "freshness": candidate["item"]["trace"]["freshness"],
                    "retrieval_trace": candidate["item"]["trace"],
                }
            )
        text = self._format(sections, request.include_historical)
        truth_states = {item["truth_state"] for item in manifest if item["included"]}
        aggregate_truth = (
            "contested"
            if "contested" in truth_states
            else "stale"
            if "stale" in truth_states
            else "resolved"
            if "resolved" in truth_states
            else "unknown"
        )
        with self.retrieval.database.session() as session:
            run = session.get(RetrievalRunRow, result["retrieval_run_id"])
            if run is not None:
                run.context_manifest = manifest
                run.selected_memory_ids = [str(item["memory_id"]) for item in selected]
            MemoryHealthService.record_retrieval(
                session,
                [str(item["memory_id"]) for item in selected],
            )
        return {
            "task": request.task,
            "repository": request.repository,
            "branch": request.branch,
            "budget": request.budget,
            "characters_used": len(text),
            "budget_exceeded": len(text) > request.budget,
            "retrieval_mode": result["pipeline_mode"],
            "retrieval_run_id": result["retrieval_run_id"],
            "query_plan": result["query_plan"],
            "truth_state": aggregate_truth,
            "sections": {key: value for key, value in sections.items() if value},
            "manifest": manifest,
            "text": text,
            "debug": {
                "config_hash": result["config_hash"],
                "scoring_profile_sha256": result.get("scoring_profile_sha256"),
                "routing_profile_sha256": result.get("routing_profile_sha256"),
                "reranker": result["reranker"],
                "candidates": manifest,
            },
        }

    def _build_msc(
        self,
        request: ContextRequest,
        legacy: dict[str, Any],
    ) -> dict[str, Any]:
        selection_started = time.perf_counter()
        counter = self._counter_for(request)
        candidates = self._candidates_from_legacy(legacy)
        metadata = self._metadata(candidates)
        atom_builder = AtomBuilder(counter)
        raw_atoms = [
            atom
            for candidate in candidates
            for atom in atom_builder.build(
                candidate,
                metadata[str(candidate["memory"]["id"])],
                requested_detail=request.detail_level,
                include_historical=request.include_historical,
            )
        ]
        atoms, _ = exact_deduplicate(raw_atoms, counter)
        intent = QueryIntent(str(legacy["query_plan"]["intent"]))
        coverage = COVERAGE[intent]
        bundles = build_bundles(atoms, coverage)
        planner = BudgetPlanner(self.settings, counter)
        decision = planner.plan(request, intent, atoms, coverage_count=len(coverage))
        policy_hash = self._compilation_policy_hash(
            decision.policy_hash,
            counter,
            request,
        )
        decision = decision.model_copy(update={"policy_hash": policy_hash})
        context_id = self.snapshot_store.reserve_id()
        retrieval_run_id = str(legacy["retrieval_run_id"])
        legacy_tokens = counter.count_json(legacy)

        required_bundles = [bundle for bundle in bundles if bundle.required]
        optional_bundles = [bundle for bundle in bundles if not bundle.required]
        atom_exclusion_reasons: dict[str, str] = {}
        selected_bundles = list(required_bundles)
        required_atoms = self._atoms_for_bundles(selected_bundles)
        required_payload = self._full_payload(
            context_id=context_id,
            retrieval_run_id=retrieval_run_id,
            atoms=required_atoms,
            counter=counter,
            legacy_equivalent_tokens=legacy_tokens,
        )
        minimum_safe_tokens = int(required_payload["usage"]["delivered_payload_tokens"])
        decision = planner.apply_safe_floor(decision, request, minimum_safe_tokens)

        for bundle in optional_bundles:
            trial_bundles = [*selected_bundles, bundle]
            trial_atoms = self._atoms_for_bundles(trial_bundles)
            trial_payload = self._full_payload(
                context_id=context_id,
                retrieval_run_id=retrieval_run_id,
                atoms=trial_atoms,
                counter=counter,
                legacy_equivalent_tokens=legacy_tokens,
            )
            if int(trial_payload["usage"]["delivered_payload_tokens"]) <= (
                decision.effective_tokens
            ):
                selected_bundles.append(bundle)
            else:
                atom_exclusion_reasons.update(
                    {atom.canonical_key: "budget" for atom in bundle.atoms}
                )

        selected_atoms = self._atoms_for_bundles(selected_bundles)
        selection_latency_ms = round((time.perf_counter() - selection_started) * 1000.0, 3)
        render_started = time.perf_counter()
        full_payload = self._full_payload(
            context_id=context_id,
            retrieval_run_id=retrieval_run_id,
            atoms=selected_atoms,
            counter=counter,
            legacy_equivalent_tokens=legacy_tokens,
            selection_latency_ms=selection_latency_ms,
        )
        render_latency_ms = round((time.perf_counter() - render_started) * 1000.0, 3)
        full_payload = self._full_payload(
            context_id=context_id,
            retrieval_run_id=retrieval_run_id,
            atoms=selected_atoms,
            counter=counter,
            legacy_equivalent_tokens=legacy_tokens,
            selection_latency_ms=selection_latency_ms,
            render_latency_ms=render_latency_ms,
        )

        removable = [bundle for bundle in selected_bundles if not bundle.required]
        while (
            int(full_payload["usage"]["delivered_payload_tokens"]) > decision.effective_tokens
            and removable
        ):
            removed_bundle = removable.pop()
            selected_bundles.remove(removed_bundle)
            atom_exclusion_reasons.update(
                {atom.canonical_key: "budget" for atom in removed_bundle.atoms}
            )
            selected_atoms = self._atoms_for_bundles(selected_bundles)
            full_payload = self._full_payload(
                context_id=context_id,
                retrieval_run_id=retrieval_run_id,
                atoms=selected_atoms,
                counter=counter,
                legacy_equivalent_tokens=legacy_tokens,
                selection_latency_ms=selection_latency_ms,
                render_latency_ms=render_latency_ms,
            )
        final_full_tokens = int(full_payload["usage"]["delivered_payload_tokens"])
        if final_full_tokens > decision.effective_tokens:
            decision = planner.apply_safe_floor(decision, request, final_full_tokens)

        delivered = full_payload
        fallback_reason: str | None = None
        previous_snapshot = None
        delta_summary: dict[str, Any] | None = None
        if request.response_mode == "full":
            if request.previous_context_id is not None:
                fallback_reason = "client_requested_full"
        elif request.previous_context_id is None:
            if request.response_mode == "delta":
                fallback_reason = "previous_context_required"
        else:
            lookup = self.snapshot_store.load_valid(
                request.previous_context_id,
                request,
                policy_hash=policy_hash,
                counter=counter,
            )
            if lookup.snapshot is None:
                fallback_reason = lookup.fallback_reason
            else:
                previous_snapshot = lookup.snapshot
                delta_plan = plan_delta(previous_snapshot, selected_atoms)
                delta_summary = delta_plan.summary()
                delta_text = render_delta(
                    added=list(delta_plan.added),
                    changed=list(delta_plan.changed),
                    removed=list(delta_plan.removed),
                )
                delta_payload = self._delta_payload(
                    context_id=context_id,
                    retrieval_run_id=retrieval_run_id,
                    base_context_id=previous_snapshot.id,
                    atoms=selected_atoms,
                    text=delta_text,
                    summary=delta_summary,
                    counter=counter,
                    full_context_tokens=final_full_tokens,
                    legacy_equivalent_tokens=legacy_tokens,
                    selection_latency_ms=selection_latency_ms,
                    render_latency_ms=render_latency_ms,
                )
                delta_tokens = int(delta_payload["usage"]["delivered_payload_tokens"])
                if (
                    delta_tokens < final_full_tokens * self.settings.context_delta_fallback_ratio
                    and delta_tokens <= decision.effective_tokens
                ):
                    delivered = delta_payload
                else:
                    fallback_reason = "delta_not_efficient"

        if delivered is full_payload and fallback_reason is not None:
            delivered["fallback_reason"] = fallback_reason
            delivered = self._stabilize_full_payload(delivered, counter)
            final_full_tokens = int(delivered["usage"]["delivered_payload_tokens"])
            if final_full_tokens > decision.effective_tokens:
                decision = planner.apply_safe_floor(decision, request, final_full_tokens)

        full_text = render_full(selected_atoms)
        base_snapshot_id = (
            previous_snapshot.id if delivered.get("mode") == "delta" and previous_snapshot else None
        )
        self.snapshot_store.create(
            context_id=context_id,
            base_snapshot_id=base_snapshot_id,
            request=request,
            policy_hash=policy_hash,
            counter=counter,
            atoms=selected_atoms,
            full_text=full_text,
            full_tokens=final_full_tokens,
        )

        compact_manifest = self._msc_manifest(
            legacy,
            raw_atoms,
            selected_atoms,
            atom_inclusion_reasons={
                atom.canonical_key: (
                    list(bundle.reasons) if bundle.reasons else ["marginal_utility_per_token"]
                )
                for bundle in selected_bundles
                for atom in bundle.atoms
            },
            atom_exclusion_reasons=atom_exclusion_reasons,
        )
        payload_breakdown = self._legacy_payload_breakdown(legacy, counter)
        self._persist_msc_audit(
            retrieval_run_id,
            request=request,
            legacy=legacy,
            delivered=delivered,
            compact_manifest=compact_manifest,
            decision=decision,
            counter=counter,
            policy_hash=policy_hash,
            payload_breakdown=payload_breakdown,
            fallback_reason=fallback_reason,
            delta_summary=delta_summary,
            selected_atoms=selected_atoms,
        )
        MSCContextResponse.model_validate(delivered)
        return delivered

    def _full_payload(
        self,
        *,
        context_id: str,
        retrieval_run_id: str,
        atoms: list[ContextAtom],
        counter: TokenCounter,
        legacy_equivalent_tokens: int,
        selection_latency_ms: float = 0.0,
        render_latency_ms: float = 0.0,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": MSC_SCHEMA_VERSION,
            "mode": "full",
            "context_id": context_id,
            "requires_base_context_id": None,
            "retrieval_run_id": retrieval_run_id,
            "truth_state": aggregate_truth_state(atoms),
            "text": render_full(atoms),
            "usage": make_usage(
                counter,
                legacy_equivalent_tokens=legacy_equivalent_tokens,
                selection_latency_ms=selection_latency_ms,
                render_latency_ms=render_latency_ms,
                **self._other_memory_operation_usage(),
            ),
        }
        return self._stabilize_full_payload(payload, counter)

    @staticmethod
    def _stabilize_full_payload(
        payload: dict[str, Any],
        counter: TokenCounter,
    ) -> dict[str, Any]:
        current = payload
        for _ in range(PAYLOAD_ACCOUNTING_MAX_ROUNDS):
            current = stabilize_payload_usage(current, counter)
            delivered = int(current["usage"]["delivered_payload_tokens"])
            if int(current["usage"]["full_context_tokens"]) == delivered:
                return current
            current["usage"]["full_context_tokens"] = delivered
        raise RuntimeError("full-context token accounting did not converge")

    def _delta_payload(
        self,
        *,
        context_id: str,
        retrieval_run_id: str,
        base_context_id: str,
        atoms: list[ContextAtom],
        text: str,
        summary: dict[str, Any],
        counter: TokenCounter,
        full_context_tokens: int,
        legacy_equivalent_tokens: int,
        selection_latency_ms: float,
        render_latency_ms: float,
    ) -> dict[str, Any]:
        return stabilize_payload_usage(
            {
                "schema_version": MSC_SCHEMA_VERSION,
                "mode": "delta",
                "context_id": context_id,
                "requires_base_context_id": base_context_id,
                "retrieval_run_id": retrieval_run_id,
                "truth_state": aggregate_truth_state(atoms),
                "text": text,
                "delta": summary,
                "usage": make_usage(
                    counter,
                    full_context_tokens=full_context_tokens,
                    legacy_equivalent_tokens=legacy_equivalent_tokens,
                    selection_latency_ms=selection_latency_ms,
                    render_latency_ms=render_latency_ms,
                    **self._other_memory_operation_usage(),
                ),
            },
            counter,
        )

    def _other_memory_operation_usage(self) -> dict[str, Any]:
        provider_enabled = bool(
            (self.settings.embedding_base_url and self.settings.embedding_model)
            or (self.settings.extractor_base_url and self.settings.reranker_model)
            or (self.settings.extractor_base_url and self.settings.relationship_model)
        )
        if provider_enabled:
            return {
                "other_memory_operation_llm_input_tokens": None,
                "other_memory_operation_llm_output_tokens": None,
                "other_memory_operation_token_attribution": (
                    MemoryOperationTokenAttribution.UNAVAILABLE
                ),
            }
        return {
            "other_memory_operation_llm_input_tokens": 0,
            "other_memory_operation_llm_output_tokens": 0,
            "other_memory_operation_token_attribution": (
                MemoryOperationTokenAttribution.EXACT_ZERO
            ),
        }

    def _counter_for(self, request: ContextRequest) -> TokenCounter:
        if request.tokenizer_id is None or request.tokenizer_id == self.token_counter.tokenizer_id:
            return self.token_counter
        if request.hard_token_budget:
            raise TokenizerUnavailableError(
                "the requested exact tokenizer is not available",
                details={
                    "requested_tokenizer_id": request.tokenizer_id,
                    "available_tokenizer_id": self.token_counter.tokenizer_id,
                    "counter_kind": self.token_counter.kind.value,
                },
            )
        return UnicodeHeuristicTokenCounter()

    @staticmethod
    def _atoms_for_bundles(bundles: list[Any]) -> list[ContextAtom]:
        atoms = [atom for bundle in bundles for atom in bundle.atoms]
        return sorted(
            atoms,
            key=lambda atom: (
                SECTION_ORDER.index(atom.section)
                if atom.section in SECTION_ORDER
                else len(SECTION_ORDER),
                -atom.utility,
                atom.memory_id,
                atom.atom_sha256,
            ),
        )

    def _compilation_policy_hash(
        self,
        budget_policy_hash: str,
        counter: TokenCounter,
        request: ContextRequest,
    ) -> str:
        return hashlib.sha256(
            canonical_json(
                {
                    "compiler": "msc-v1",
                    "budget_policy_hash": budget_policy_hash,
                    "counter_fingerprint": counter_fingerprint(counter),
                    "detail_level": request.detail_level.value,
                    "include_historical": request.include_historical,
                    "budget_tokens": request.budget_tokens,
                    "budget_profile": request.budget_profile.value,
                    "hard_token_budget": request.hard_token_budget,
                    "exact_dedup": "canonical-v1",
                }
            ).encode("utf-8")
        ).hexdigest()

    def _candidates_from_legacy(self, legacy: dict[str, Any]) -> list[dict[str, Any]]:
        manifests = list(legacy.get("manifest", []))
        memory_ids = [str(item["memory_id"]) for item in manifests]
        if not memory_ids:
            return []
        with self.retrieval.database.session() as session:
            rows = list(session.scalars(select(MemoryRow).where(MemoryRow.id.in_(memory_ids))))
        by_id = {row.id: self._serialize_memory_row(row) for row in rows}
        candidates = []
        for item in manifests:
            memory_id = str(item["memory_id"])
            memory = by_id.get(memory_id)
            if memory is None:
                continue
            candidates.append(
                {
                    "memory": memory,
                    "score": float(item.get("utility", 0.0)),
                    "truth_state": str(item["truth_state"]),
                    "claim_ids": list(item.get("claim_ids", [])),
                    "trace": dict(item["retrieval_trace"]),
                }
            )
        return candidates

    @staticmethod
    def _serialize_memory_row(row: MemoryRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "scope_type": row.scope_type.value,
            "scope_key": row.scope_key,
            "memory_type": row.memory_type.value,
            "category": row.category,
            "subject": row.subject,
            "key": row.key,
            "title": row.title,
            "content": row.content,
            "status": row.status.value,
            "confidence": row.confidence,
            "importance": row.importance,
            "valid_from": row.valid_from.isoformat() if row.valid_from else None,
            "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "ttl_seconds": row.ttl_seconds,
            "supersedes_id": row.supersedes_id,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "created_by": row.created_by.value,
            "sensitivity": row.sensitivity.value,
            "metadata": row.metadata_json,
        }

    @staticmethod
    def _legacy_payload_breakdown(
        legacy: dict[str, Any],
        counter: TokenCounter,
    ) -> dict[str, Any]:
        return {
            "counter_kind": counter.kind.value,
            "tokenizer_id": counter.tokenizer_id,
            "text_tokens": counter.count_text(str(legacy.get("text", ""))),
            "sections_tokens": counter.count_json(legacy.get("sections", {})),
            "manifest_tokens": counter.count_json(legacy.get("manifest", [])),
            "debug_tokens": counter.count_json(legacy.get("debug", {})),
            "total_payload_tokens": counter.count_json(legacy),
            "characters_used_semantics": "legacy_text_characters",
        }

    @staticmethod
    def _msc_manifest(
        legacy: dict[str, Any],
        raw_atoms: list[ContextAtom],
        selected_atoms: list[ContextAtom],
        *,
        atom_inclusion_reasons: dict[str, list[str]] | None = None,
        atom_exclusion_reasons: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        inclusion_reasons = atom_inclusion_reasons or {}
        exclusion_reasons = atom_exclusion_reasons or {}
        raw_by_memory: dict[str, list[ContextAtom]] = defaultdict(list)
        for atom in raw_atoms:
            raw_by_memory[atom.memory_id].append(atom)
        selected_by_memory: dict[str, list[str]] = defaultdict(list)
        for atom in selected_atoms:
            for memory_id in atom.memory_ids:
                selected_by_memory[memory_id].append(atom.atom_sha256)
        selected_memory_ids = {
            memory_id for atom in selected_atoms for memory_id in atom.memory_ids
        }
        selected_primary_memory_ids = {atom.memory_id for atom in selected_atoms}
        selected_by_canonical = {atom.canonical_key: atom for atom in selected_atoms}
        duplicate_primaries: dict[str, set[str]] = defaultdict(set)
        for atom in raw_atoms:
            selected = selected_by_canonical.get(atom.canonical_key)
            if (
                selected is not None
                and atom.memory_id != selected.memory_id
                and atom.memory_id in selected.memory_ids
            ):
                duplicate_primaries[atom.memory_id].add(selected.memory_id)
        result = []
        for item in legacy.get("manifest", []):
            memory_id = str(item["memory_id"])
            duplicate_primary_ids = sorted(duplicate_primaries[memory_id])
            included = memory_id in selected_primary_memory_ids
            atom_values = raw_by_memory.get(memory_id, [])
            atom_manifest = []
            for atom in atom_values:
                selected = selected_by_canonical.get(atom.canonical_key)
                atom_included = selected is not None and selected.memory_id == memory_id
                duplicate_primary = (
                    selected.memory_id
                    if selected is not None and selected.memory_id != memory_id
                    else None
                )
                atom_manifest.append(
                    {
                        "atom_sha256": atom.atom_sha256,
                        "canonical_key": atom.canonical_key,
                        "selected_atom_sha256": (
                            selected.atom_sha256 if selected is not None else None
                        ),
                        "included": atom_included,
                        "inclusion_reasons": (
                            inclusion_reasons.get(atom.canonical_key, [])
                            if atom_included
                            else ["duplicate_evidence_merged"]
                            if duplicate_primary is not None
                            else []
                        ),
                        "exclusion_reason": (
                            None
                            if atom_included
                            else "duplicate"
                            if duplicate_primary is not None
                            else "stale"
                            if atom.freshness.value == "stale"
                            else exclusion_reasons.get(
                                atom.canonical_key,
                                "lower_utility",
                            )
                        ),
                        "duplicate_of_memory_id": duplicate_primary,
                    }
                )
            memory_exclusion_reasons = {
                str(value["exclusion_reason"])
                for value in atom_manifest
                if value["exclusion_reason"] is not None
            }
            result.append(
                {
                    "memory_id": memory_id,
                    "atom_sha256": [atom.atom_sha256 for atom in atom_values],
                    "atoms": atom_manifest,
                    "included": included,
                    "selected_atom_sha256": sorted(set(selected_by_memory[memory_id])),
                    "exclusion_reason": (
                        None
                        if included
                        else "duplicate"
                        if duplicate_primary_ids and memory_exclusion_reasons == {"duplicate"}
                        else sorted(memory_exclusion_reasons)[0]
                        if memory_exclusion_reasons
                        else "lower_utility"
                    ),
                    "duplicate_of_memory_id": (
                        duplicate_primary_ids[0] if duplicate_primary_ids else None
                    ),
                    "duplicate_of_memory_ids": duplicate_primary_ids,
                    "represented_in_selected_context": memory_id in selected_memory_ids,
                    "truth_state": item["truth_state"],
                    "freshness": item["freshness"],
                }
            )
        return result

    def _persist_msc_audit(
        self,
        retrieval_run_id: str,
        *,
        request: ContextRequest,
        legacy: dict[str, Any],
        delivered: dict[str, Any],
        compact_manifest: list[dict[str, Any]],
        decision: BudgetDecision,
        counter: TokenCounter,
        policy_hash: str,
        payload_breakdown: dict[str, Any],
        fallback_reason: str | None,
        delta_summary: dict[str, Any] | None,
        selected_atoms: list[ContextAtom],
    ) -> None:
        policy_manifest = {
            "compiler_mode": self.settings.context_compiler_mode,
            "compiler_version": "msc-v1",
            "budget": decision.model_dump(mode="json"),
            "policy_hash": policy_hash,
            "counter_fingerprint": counter_fingerprint(counter),
            "requested_tokenizer_id": request.tokenizer_id,
            "response_mode": request.response_mode,
            "detail_level": request.detail_level.value,
            "semantic_dedup": "shadow_only",
            "context_compilation_llm_tokens": 0,
        }
        with self.retrieval.database.session() as session:
            run = session.get(RetrievalRunRow, retrieval_run_id)
            if run is None:
                return
            run.context_usage_json = dict(delivered["usage"])
            run.context_policy_manifest = policy_manifest
            run.context_diagnostics_json = {
                "query_plan": legacy["query_plan"],
                "sections": legacy["sections"],
                "legacy_manifest": legacy["manifest"],
                "msc_manifest": compact_manifest,
                "candidate_features": run.candidate_features,
                "reranker": legacy["debug"]["reranker"],
                "legacy_payload_breakdown": payload_breakdown,
                "fallback_reason": fallback_reason,
                "delta": delta_summary,
                "selected_atoms": [atom.model_dump(mode="json") for atom in selected_atoms],
            }
            run.context_shadow_json = {
                "payload": delivered,
                "mode": self.settings.context_compiler_mode,
            }
            if self.settings.context_compiler_mode == "msc":
                run.context_manifest = compact_manifest
                run.selected_memory_ids = sorted(
                    {memory_id for atom in selected_atoms for memory_id in atom.memory_ids}
                )

    def _save_shadow_error(self, retrieval_run_id: str, exc: Exception) -> None:
        if isinstance(exc, MemoryOSError):
            error = exc.as_dict()
        else:
            logger.exception(
                "MSC shadow compilation failed for retrieval run %s",
                retrieval_run_id,
            )
            error = {
                "code": "MSC_SHADOW_FAILURE",
                "message": "MSC shadow compilation failed; the legacy response was preserved",
                "details": {"exception_type": type(exc).__name__},
            }
        with self.retrieval.database.session() as session:
            run = session.get(RetrievalRunRow, retrieval_run_id)
            if run is not None:
                run.context_shadow_json = {
                    "mode": "msc_shadow",
                    "error": error,
                }

    @staticmethod
    def _validate_score_contract(result: dict[str, Any]) -> None:
        query_plan = result.get("query_plan")
        routing = query_plan.get("routing") if isinstance(query_plan, dict) else None
        if not isinstance(routing, dict):
            raise ValueError("retrieval result omitted its routing score contract")
        routed = result.get("routing_profile_sha256") is not None
        expected = NORMALIZED_SCORE_CONTRACT if routed else LEGACY_SCORE_CONTRACT
        if routing.get("score_contract") != expected:
            raise ValueError("retrieval result used an unsupported score contract")
        if not routed:
            return
        items = result.get("items")
        if not isinstance(items, list):
            raise ValueError("routed retrieval result omitted candidate items")
        for item in items:
            trace = item.get("trace") if isinstance(item, dict) else None
            fused_score = trace.get("fused_score") if isinstance(trace, dict) else None
            if (
                isinstance(fused_score, bool)
                or not isinstance(fused_score, (int, float))
                or not math.isfinite(float(fused_score))
                or not 0.0 <= float(fused_score) <= 1.0
            ):
                raise ValueError("routed retrieval violated the normalized fusion contract")

    def _metadata(self, candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        memory_ids = [str(item["memory"]["id"]) for item in candidates]
        result: dict[str, dict[str, Any]] = {
            memory_id: {
                "source_ref": "unknown",
                "source_refs": [],
                "claim_groups": [],
                "claims": [],
                "evidence_pointers": [],
            }
            for memory_id in memory_ids
        }
        if not memory_ids:
            return result
        with self.retrieval.database.session() as session:
            source_rows = session.execute(
                select(MemorySourceRow.memory_id, SourceRow)
                .join(SourceRow, SourceRow.id == MemorySourceRow.source_id)
                .where(MemorySourceRow.memory_id.in_(memory_ids))
                .order_by(SourceRow.captured_at.desc())
            )
            for memory_id, source in source_rows:
                source_ref = source.source_ref
                if result[memory_id]["source_ref"] == "unknown":
                    result[memory_id]["source_ref"] = source_ref
                result[memory_id]["source_refs"].append(source_ref)
                result[memory_id]["evidence_pointers"].append(
                    {
                        "source_id": source.id,
                        "source_ref": source.source_ref,
                        "content_hash": source.content_hash,
                        "captured_at": source.captured_at.isoformat(),
                    }
                )

            claims = list(
                session.scalars(select(ClaimRow).where(ClaimRow.memory_id.in_(memory_ids)))
            )
            entity_ids = {
                value
                for claim in claims
                for value in (claim.subject_entity_id, claim.object_entity_id)
                if value is not None
            }
            entities = {
                row.id: row.canonical_name
                for row in session.scalars(select(EntityRow).where(EntityRow.id.in_(entity_ids)))
            }
            claim_to_memory: dict[str, str] = {}
            for claim in claims:
                memory_id = claim.memory_id
                claim_to_memory[claim.id] = memory_id
                result[memory_id]["claim_groups"].append(
                    f"{claim.subject_entity_id}:{claim.predicate}"
                )
                result[memory_id]["claims"].append(
                    {
                        "id": claim.id,
                        "subject_entity_id": claim.subject_entity_id,
                        "subject": entities.get(claim.subject_entity_id),
                        "predicate": claim.predicate,
                        "object_kind": claim.object_kind.value,
                        "object_entity_id": claim.object_entity_id,
                        "object_name": entities.get(claim.object_entity_id)
                        if claim.object_entity_id
                        else None,
                        "object_value": claim.object_value,
                        "polarity": claim.polarity.value,
                        "modality": claim.modality.value,
                        "qualifiers": claim.qualifiers_json,
                        "status": claim.status.value,
                        "valid_from": claim.valid_from.isoformat() if claim.valid_from else None,
                        "valid_to": claim.valid_to.isoformat() if claim.valid_to else None,
                        "recorded_at": claim.recorded_at.isoformat(),
                        "stale_state": claim.stale_state.value,
                    }
                )
            if claim_to_memory:
                evidence_rows = session.execute(
                    select(ClaimEvidenceRow, SourceRow, SourceAnchorRow)
                    .join(SourceRow, SourceRow.id == ClaimEvidenceRow.source_id)
                    .outerjoin(
                        SourceAnchorRow,
                        SourceAnchorRow.id == ClaimEvidenceRow.source_anchor_id,
                    )
                    .where(ClaimEvidenceRow.claim_id.in_(list(claim_to_memory)))
                )
                for evidence, source, anchor in evidence_rows:
                    memory_id = claim_to_memory[evidence.claim_id]
                    result[memory_id]["evidence_pointers"].append(
                        {
                            "claim_id": evidence.claim_id,
                            "source_id": source.id,
                            "source_ref": source.source_ref,
                            "evidence_hash": evidence.evidence_hash,
                            "anchor_id": anchor.id if anchor else None,
                            "commit_sha": anchor.commit_sha if anchor else None,
                            "path": anchor.path if anchor else None,
                            "line_start": anchor.line_start if anchor else None,
                            "line_end": anchor.line_end if anchor else None,
                            "freshness": anchor.freshness_state.value if anchor else None,
                            "observed_path": anchor.observed_path if anchor else None,
                            "observed_line_start": (anchor.observed_line_start if anchor else None),
                            "observed_line_end": anchor.observed_line_end if anchor else None,
                            "observed_excerpt_hash": (
                                anchor.observed_excerpt_hash if anchor else None
                            ),
                        }
                    )
        return result

    @staticmethod
    def _format(sections: dict[str, list[dict[str, Any]]], include_historical: bool) -> str:
        formatted = ["Project Memory Context"]
        for name in SECTION_ORDER:
            if name == "HISTORICAL / SUPERSEDED" and not include_historical:
                continue
            items = sections[name]
            if not items:
                continue
            formatted.append(f"\n{name}")
            for item in items:
                prefix = "CONTESTED: " if item["truth_state"] == "contested" else ""
                if item["freshness"] == "suspect":
                    prefix += "SUSPECT: "
                formatted.append(
                    f"- [{item['id']}] {prefix}{item['title']}: {item['content']} "
                    f"(source: {item['provenance_ref']})"
                )
        return "\n".join(formatted)
