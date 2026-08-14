from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from memoryos.config import MemoryOSSettings
from memoryos.context.atoms import CompressionPolicy, ContextAtom
from memoryos.context.renderers import render_full_items
from memoryos.context.token_meter import TokenCounter, canonical_json
from memoryos.db.models import ContextSnapshotRow, new_id
from memoryos.db.session import Database
from memoryos.domain.schemas import ContextRequest


class ContextSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    base_snapshot_id: str | None
    request_fingerprint: str
    scope_fingerprint: str
    policy_hash: str
    tokenizer_id: str
    counter_kind: str
    items: list[dict[str, Any]]
    full_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    full_estimated_tokens: int = Field(ge=0)
    created_at: datetime
    expires_at: datetime


class SnapshotLookup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: ContextSnapshot | None = None
    fallback_reason: str | None = None


class DeltaPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    added: tuple[ContextAtom, ...]
    changed: tuple[ContextAtom, ...]
    removed: tuple[dict[str, Any], ...]
    unchanged_count: int = Field(ge=0)

    def summary(self) -> dict[str, Any]:
        return {
            "added": sorted({atom.memory_id for atom in self.added}),
            "changed": sorted({atom.memory_id for atom in self.changed}),
            "removed": sorted({str(item["memory_id"]) for item in self.removed}),
            "unchanged_count": self.unchanged_count,
        }


class ContextSnapshotStore:
    def __init__(self, database: Database, settings: MemoryOSSettings) -> None:
        self.database = database
        self.settings = settings

    @staticmethod
    def reserve_id() -> str:
        return new_id()

    def load_valid(
        self,
        snapshot_id: str,
        request: ContextRequest,
        *,
        policy_hash: str,
        counter: TokenCounter,
        now: datetime | None = None,
    ) -> SnapshotLookup:
        current = _utc(now or datetime.now(UTC))
        scope_hash = scope_fingerprint(request)
        with self.database.session() as session:
            row = session.get(ContextSnapshotRow, snapshot_id)
            if row is None:
                result = SnapshotLookup(fallback_reason="snapshot_unavailable")
            elif _utc(row.expires_at) <= current:
                result = SnapshotLookup(fallback_reason="snapshot_expired")
            elif row.scope_fingerprint != scope_hash:
                result = SnapshotLookup(fallback_reason="scope_mismatch")
            elif row.tokenizer_id != counter.tokenizer_id or row.counter_kind != counter.kind.value:
                result = SnapshotLookup(fallback_reason="tokenizer_mismatch")
            elif row.policy_hash != policy_hash:
                result = SnapshotLookup(fallback_reason="policy_mismatch")
            else:
                reconstructed = render_full_items(row.items_json)
                reconstructed_hash = hashlib.sha256(reconstructed.encode("utf-8")).hexdigest()
                if reconstructed_hash != row.full_text_sha256:
                    result = SnapshotLookup(fallback_reason="snapshot_integrity_failure")
                else:
                    result = SnapshotLookup(snapshot=_serialize_snapshot(row))
        self.cleanup_expired(scope_hash, now=current)
        return result

    def create(
        self,
        *,
        context_id: str,
        base_snapshot_id: str | None,
        request: ContextRequest,
        policy_hash: str,
        counter: TokenCounter,
        atoms: list[ContextAtom],
        full_text: str,
        full_tokens: int,
        now: datetime | None = None,
    ) -> ContextSnapshot:
        current = _utc(now or datetime.now(UTC))
        expires = current + timedelta(seconds=self.settings.context_snapshot_ttl_seconds)
        items = [atom.snapshot_item(index) for index, atom in enumerate(atoms)]
        row = ContextSnapshotRow(
            id=context_id,
            base_snapshot_id=base_snapshot_id,
            request_fingerprint=request_fingerprint(request),
            scope_fingerprint=scope_fingerprint(request),
            policy_hash=policy_hash,
            tokenizer_id=counter.tokenizer_id,
            counter_kind=counter.kind.value,
            items_json=items,
            full_text_sha256=hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
            full_estimated_tokens=full_tokens,
            created_at=current,
            expires_at=expires,
        )
        with self.database.session() as session:
            session.add(row)
            session.flush()
            result = _serialize_snapshot(row)
        return result

    def cleanup_expired(
        self,
        scope_hash: str,
        *,
        now: datetime | None = None,
    ) -> int:
        current = _utc(now or datetime.now(UTC))
        with self.database.session() as session:
            ids = list(
                session.scalars(
                    select(ContextSnapshotRow.id)
                    .where(
                        ContextSnapshotRow.scope_fingerprint == scope_hash,
                        ContextSnapshotRow.expires_at <= current,
                    )
                    .order_by(ContextSnapshotRow.expires_at.asc())
                    .limit(self.settings.context_snapshot_cleanup_batch_size)
                )
            )
            if ids:
                session.execute(delete(ContextSnapshotRow).where(ContextSnapshotRow.id.in_(ids)))
            return len(ids)


def plan_delta(previous: ContextSnapshot, current: list[ContextAtom]) -> DeltaPlan:
    old_by_key = {_item_key(item): item for item in previous.items}
    current_by_key = {_atom_key(atom): atom for atom in current}
    added_keys = set(current_by_key) - set(old_by_key)
    removed_keys = set(old_by_key) - set(current_by_key)
    shared = set(old_by_key) & set(current_by_key)
    changed_keys = {
        key
        for key in shared
        if str(old_by_key[key]["atom_sha256"]) != current_by_key[key].atom_sha256
        or str(old_by_key[key].get("evidence_pointer_version", ""))
        != current_by_key[key].evidence_pointer_version
    }
    unchanged_keys = shared - changed_keys

    changed_bundle_keys = {
        _bundle_key_for_current(current_by_key[key]) for key in added_keys | changed_keys
    }
    changed_bundle_keys.update(str(old_by_key[key].get("bundle_key", "")) for key in removed_keys)
    safety_bundles = {
        atom.bundle_key
        for atom in current
        if atom.compression_policy is CompressionPolicy.PINNED
        or atom.truth_state.value == "contested"
    }
    safety_bundles.update(
        str(item.get("bundle_key", ""))
        for item in previous.items
        if item.get("compression_policy") == CompressionPolicy.PINNED.value
        or item.get("truth_state") == "contested"
    )
    expanded = changed_bundle_keys & safety_bundles
    if expanded:
        for key, atom in current_by_key.items():
            if atom.bundle_key not in expanded:
                continue
            if key in old_by_key:
                changed_keys.add(key)
                unchanged_keys.discard(key)
            else:
                added_keys.add(key)

    return DeltaPlan(
        added=tuple(sorted((current_by_key[key] for key in added_keys), key=_atom_order)),
        changed=tuple(sorted((current_by_key[key] for key in changed_keys), key=_atom_order)),
        removed=tuple(
            sorted((old_by_key[key] for key in removed_keys), key=lambda item: _item_key(item))
        ),
        unchanged_count=len(unchanged_keys),
    )


def scope_fingerprint(request: ContextRequest) -> str:
    return _fingerprint(
        {
            "repository": request.repository,
            "branch": request.branch,
            "workspace": request.workspace,
            "task_scope": request.task_scope,
        }
    )


def request_fingerprint(request: ContextRequest) -> str:
    return _fingerprint(
        {
            "scope": scope_fingerprint(request),
            "task": request.task,
            "include_historical": request.include_historical,
            "as_of_valid_time": request.as_of_valid_time,
            "as_known_at": request.as_known_at,
            "detail_level": request.detail_level.value,
        }
    )


def _serialize_snapshot(row: ContextSnapshotRow) -> ContextSnapshot:
    return ContextSnapshot(
        id=row.id,
        base_snapshot_id=row.base_snapshot_id,
        request_fingerprint=row.request_fingerprint,
        scope_fingerprint=row.scope_fingerprint,
        policy_hash=row.policy_hash,
        tokenizer_id=row.tokenizer_id,
        counter_kind=row.counter_kind,
        items=row.items_json,
        full_text_sha256=row.full_text_sha256,
        full_estimated_tokens=row.full_estimated_tokens,
        created_at=_utc(row.created_at),
        expires_at=_utc(row.expires_at),
    )


def _atom_key(atom: ContextAtom) -> str:
    return f"{atom.memory_id}|{atom.bundle_key}"


def _item_key(item: dict[str, Any]) -> str:
    return f"{item['memory_id']}|{item.get('bundle_key', '')}"


def _bundle_key_for_current(atom: ContextAtom) -> str:
    return atom.bundle_key


def _atom_order(atom: ContextAtom) -> tuple[str, str]:
    return atom.section, atom.memory_id


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "ContextSnapshot",
    "ContextSnapshotStore",
    "DeltaPlan",
    "SnapshotLookup",
    "plan_delta",
    "request_fingerprint",
    "scope_fingerprint",
]
