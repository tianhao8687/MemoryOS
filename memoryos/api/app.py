from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from memoryos.backup import BackupService
from memoryos.config import MemoryOSSettings
from memoryos.db.models import RepositoryRow
from memoryos.db.session import Database
from memoryos.doctor import run_doctor
from memoryos.domain.schemas import (
    ConflictStrategy,
    ConsolidateRequest,
    ContextRequest,
    CreatedBy,
    CurrentTruthRequest,
    FeedbackCreate,
    MemoryCreate,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
    RefreshRequest,
    ScopeType,
    SearchRequest,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService
from memoryos.errors import AuthenticationError, MemoryOSError, OriginRejectedError
from memoryos.evaluation.report import load_memorybench_report
from memoryos.integrations.git import discover_git_context, upsert_repository
from memoryos.providers.base import CandidateExtractor
from memoryos.providers.heuristic import HeuristicExtractor
from memoryos.providers.openai_compatible import OpenAICompatibleExtractor
from memoryos.security.logging import configure_logging
from memoryos.security.token import TokenManager


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: ConflictStrategy | None = None
    rationale: str | None = Field(default=None, max_length=2000)


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=50000)
    scope_type: ScopeType
    scope_key: str
    source_type: SourceType = SourceType.CONVERSATION
    source_ref: str = Field(min_length=1, max_length=1000)


class GitDetectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = "."


class SourceAnchorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_path: str = Field(min_length=1, max_length=2000)
    path: str = Field(min_length=1, max_length=2000)
    symbol_fqn: str | None = Field(default=None, max_length=1000)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)


def _origin_allowed(origin: str | None, settings: MemoryOSSettings) -> bool:
    if origin is None:
        return True
    if origin in settings.allowed_origins:
        return True
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def create_app(settings: MemoryOSSettings) -> FastAPI:
    settings.ensure_directories()
    configure_logging(settings)
    database = Database(settings)
    database.initialize()
    service = MemoryService(database, settings)
    backup = BackupService(database, settings)
    token_manager = TokenManager(settings.token_path)
    token_manager.get_or_create()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        database.close()

    app = FastAPI(
        title="MemoryOS",
        version="2.0.0",
        description="Local-first truth and memory intelligence for coding agents",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.service = service
    app.state.backup = backup
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    async def require_write_access(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        origin = request.headers.get("origin")
        if not _origin_allowed(origin, settings):
            raise OriginRejectedError("write request Origin is not in the localhost allowlist")
        candidate = None
        if authorization and authorization.lower().startswith("bearer "):
            candidate = authorization[7:].strip()
        candidate = candidate or request.cookies.get("memoryos_session")
        if not token_manager.verify(candidate):
            raise AuthenticationError("a valid local bearer token is required for write operations")

    @app.exception_handler(MemoryOSError)
    async def memoryos_error_handler(_request: Request, exc: MemoryOSError) -> JSONResponse:
        code_to_status = {
            "NOT_FOUND": status.HTTP_404_NOT_FOUND,
            "AUTH_REQUIRED": status.HTTP_401_UNAUTHORIZED,
            "ORIGIN_REJECTED": status.HTTP_403_FORBIDDEN,
            "CONFLICT_DETECTED": status.HTTP_409_CONFLICT,
            "INVALID_TRANSITION": status.HTTP_409_CONFLICT,
            "PROVIDER_FAILURE": status.HTTP_502_BAD_GATEWAY,
            "BACKUP_ERROR": status.HTTP_400_BAD_REQUEST,
        }
        return JSONResponse(
            status_code=code_to_status.get(exc.code, status.HTTP_400_BAD_REQUEST),
            content={"ok": False, "error": exc.as_dict()},
        )

    @app.exception_handler(ValueError)
    async def validation_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "ok": False,
                "error": {"code": "VALIDATION_ERROR", "message": str(exc), "details": {}},
            },
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": "2.0.0", "database": database.integrity_check()}

    @app.get("/api/status")
    def get_status() -> dict[str, Any]:
        return service.status()

    @app.get("/api/doctor")
    def doctor() -> dict[str, Any]:
        return run_doctor(database, settings)

    @app.get("/api/memories")
    def list_memories(
        q: str = "",
        scope_type: ScopeType | None = None,
        scope_key: str | None = None,
        memory_type: MemoryType | None = None,
        memory_status: Annotated[MemoryStatus | None, Query(alias="status")] = None,
        include_history: bool = False,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        return service.search(
            SearchRequest(
                query=q,
                scope_type=scope_type,
                scope_key=scope_key,
                memory_type=memory_type,
                status=memory_status,
                include_history=include_history,
                limit=limit,
                offset=offset,
            )
        )

    @app.post("/api/memories", dependencies=[Depends(require_write_access)])
    def propose_memory(payload: MemoryCreate) -> dict[str, Any]:
        return {"ok": True, "memory": service.propose(payload, actor="http")}

    @app.get("/api/memories/{memory_id}")
    def get_memory(memory_id: str) -> dict[str, Any]:
        return service.get(memory_id)

    @app.put("/api/memories/{memory_id}", dependencies=[Depends(require_write_access)])
    def edit_memory(memory_id: str, payload: MemoryUpdate) -> dict[str, Any]:
        return {"ok": True, "memory": service.update(memory_id, payload, actor="http")}

    @app.post("/api/memories/{memory_id}/confirm", dependencies=[Depends(require_write_access)])
    def confirm_memory(memory_id: str, payload: ConfirmRequest) -> dict[str, Any]:
        return {
            "ok": True,
            "memory": service.confirm(
                memory_id, strategy=payload.strategy, actor="http", rationale=payload.rationale
            ),
        }

    @app.post("/api/memories/{memory_id}/reject", dependencies=[Depends(require_write_access)])
    def reject_memory(memory_id: str) -> dict[str, Any]:
        return {"ok": True, "memory": service.reject(memory_id, actor="http")}

    @app.post("/api/memories/{memory_id}/forget", dependencies=[Depends(require_write_access)])
    def forget_memory(memory_id: str) -> dict[str, Any]:
        return {"ok": True, "memory": service.forget(memory_id, actor="http")}

    @app.get("/api/memories/{memory_id}/explain")
    def explain_memory(memory_id: str) -> dict[str, Any]:
        return service.explain(memory_id)

    @app.get("/api/memories/{memory_id}/history")
    def memory_history(memory_id: str) -> list[dict[str, Any]]:
        return service.history(memory_id=memory_id)

    @app.post("/api/context")
    def memory_context(payload: ContextRequest) -> dict[str, Any]:
        return service.context(payload)

    @app.post("/api/current-truth")
    def current_truth(payload: CurrentTruthRequest) -> dict[str, Any]:
        return service.current_truth(payload)

    @app.post("/api/claim-graph")
    def claim_graph(payload: CurrentTruthRequest) -> dict[str, Any]:
        return service.claim_graph(payload)

    @app.post("/api/debug/context")
    def debug_context(payload: ContextRequest) -> dict[str, Any]:
        return service.debug_context(payload)

    @app.get("/api/retrieval-runs/{run_id}")
    def retrieval_run(run_id: str) -> dict[str, Any]:
        return service.retrieval_run(run_id)

    @app.post("/api/feedback", dependencies=[Depends(require_write_access)])
    def memory_feedback(payload: FeedbackCreate) -> dict[str, Any]:
        return {"ok": True, "feedback": service.feedback(payload)}

    @app.post("/api/consolidate", dependencies=[Depends(require_write_access)])
    def memory_consolidate(payload: ConsolidateRequest) -> dict[str, Any]:
        return {"ok": True, **service.consolidate(payload)}

    @app.get("/api/consolidations")
    def consolidation_inbox(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return service.consolidation_inbox(limit=limit)

    @app.post("/api/refresh", dependencies=[Depends(require_write_access)])
    def memory_refresh(payload: RefreshRequest) -> dict[str, Any]:
        return {"ok": True, "refresh": service.refresh_memory(payload)}

    @app.get("/api/freshness")
    def freshness(limit: int = Query(default=200, ge=1, le=500)) -> list[dict[str, Any]]:
        return service.freshness(limit=limit)

    @app.get("/api/benchmarks/memorybench-v2")
    def memorybench_v2_report() -> dict[str, Any]:
        return load_memorybench_report()

    @app.post("/api/memories/{memory_id}/anchors", dependencies=[Depends(require_write_access)])
    def create_source_anchor(memory_id: str, payload: SourceAnchorRequest) -> dict[str, Any]:
        return {
            "ok": True,
            "anchor": service.create_source_anchor(
                memory_id=memory_id,
                repository_path=payload.repository_path,
                path=payload.path,
                symbol_fqn=payload.symbol_fqn,
                line_start=payload.line_start,
                line_end=payload.line_end,
            ),
        }

    @app.get("/api/conflicts")
    def get_conflicts(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return service.conflicts(limit=limit)

    @app.post("/api/conflicts/{candidate_id}/resolve", dependencies=[Depends(require_write_access)])
    def resolve_conflict(candidate_id: str, payload: ConfirmRequest) -> dict[str, Any]:
        if payload.strategy is None:
            raise HTTPException(status_code=422, detail="strategy is required")
        return {
            "ok": True,
            "memory": service.confirm(
                candidate_id, strategy=payload.strategy, actor="http", rationale=payload.rationale
            ),
        }

    @app.get("/api/timeline")
    def timeline(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return service.timeline(limit=limit)

    @app.get("/api/audit")
    def audit(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return service.timeline(limit=limit)

    @app.post("/api/extract", dependencies=[Depends(require_write_access)])
    def extract_candidates(payload: ExtractRequest) -> dict[str, Any]:
        extractor: CandidateExtractor
        if settings.extractor_base_url and settings.extractor_model:
            extractor = OpenAICompatibleExtractor(
                base_url=settings.extractor_base_url,
                model=settings.extractor_model,
                api_key=settings.extractor_api_key,
                timeout=settings.provider_timeout_seconds,
                max_input_chars=settings.provider_max_input_chars,
            )
        else:
            extractor = HeuristicExtractor()
        candidates = extractor.extract(payload.text)
        created = []
        for candidate in candidates:
            created.append(
                service.propose(
                    MemoryCreate(
                        scope_type=payload.scope_type,
                        scope_key=payload.scope_key,
                        memory_type=candidate.memory_type,
                        category=candidate.category,
                        subject=candidate.subject,
                        key=candidate.key,
                        title=candidate.title,
                        content=candidate.content,
                        confidence=candidate.confidence,
                        importance=candidate.importance,
                        ttl_seconds=candidate.ttl_seconds,
                        claim_candidates=candidate.claim_candidates,
                        created_by=CreatedBy.EXTRACTOR,
                        source=SourceCreate(
                            source_type=payload.source_type,
                            source_ref=payload.source_ref,
                            excerpt=payload.text,
                        ),
                    ),
                    actor=f"{extractor.metadata.provider}:{extractor.metadata.model}",
                )
            )
        return {"ok": True, "candidates": created, "count": len(created)}

    @app.get("/api/repositories")
    def repositories() -> list[dict[str, Any]]:
        with database.session() as session:
            rows = list(
                session.scalars(select(RepositoryRow).order_by(RepositoryRow.updated_at.desc()))
            )
            return [
                {
                    "id": row.id,
                    "stable_key": row.stable_key,
                    "name": row.name,
                    "path": row.path,
                    "remote_url": row.remote_url,
                    "default_branch": row.default_branch,
                }
                for row in rows
            ]

    @app.post("/api/repositories/detect", dependencies=[Depends(require_write_access)])
    def detect_repository(payload: GitDetectRequest) -> dict[str, str | None]:
        return upsert_repository(database, discover_git_context(payload.path))

    @app.post("/api/backup", dependencies=[Depends(require_write_access)])
    def create_backup() -> dict[str, Any]:
        path = backup.create_backup()
        return {"ok": True, "path": str(path)}

    @app.post("/api/export", dependencies=[Depends(require_write_access)])
    def export_data() -> dict[str, Any]:
        destination = settings.data_dir / "exports" / "memoryos-export.zip"
        path = backup.export_jsonl(destination)
        return {"ok": True, "path": str(path)}

    @app.get("/api/settings")
    def public_settings() -> dict[str, Any]:
        return {
            "database_path": str(settings.database_path),
            "backup_path": str(settings.backup_dir),
            "mcp_status": "available",
            "provider_status": "configured"
            if settings.extractor_base_url or settings.embedding_base_url
            else "offline",
            "host": settings.host,
            "telemetry": False,
            "version": "2.0.0",
            "provider_capabilities": [
                "candidate_extraction",
                "claim_extraction",
                "embedding",
                "relationship_judgement",
                "rerank",
                "consolidation_judgement",
                "staleness_judgement",
            ],
        }

    @app.get("/", include_in_schema=False)
    def index(response: Response) -> Response:
        if (settings.web_dist / "index.html").exists():
            result: Response = FileResponse(settings.web_dist / "index.html")
        else:
            result = JSONResponse({"name": "MemoryOS", "status": "UI not built", "api": "/docs"})
        result.set_cookie(
            "memoryos_session",
            token_manager.get_or_create(),
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return result

    @app.get("/{asset_path:path}", include_in_schema=False)
    def static_assets(asset_path: str) -> Response:
        root = settings.web_dist.resolve()
        candidate = (root / asset_path).resolve()
        if root not in candidate.parents and candidate != root:
            raise HTTPException(status_code=404)
        if candidate.is_file():
            return FileResponse(candidate)
        index_path = root / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        raise HTTPException(status_code=404)

    return app
