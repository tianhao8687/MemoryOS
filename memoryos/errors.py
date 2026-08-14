from __future__ import annotations

from typing import Any


class MemoryOSError(Exception):
    """Base exception with a stable, transport-safe error code."""

    code = "MEMORYOS_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFoundError(MemoryOSError):
    code = "NOT_FOUND"


class InvalidTransitionError(MemoryOSError):
    code = "INVALID_TRANSITION"


class ConflictDetectedError(MemoryOSError):
    code = "CONFLICT_DETECTED"


class ValidationError(MemoryOSError):
    code = "VALIDATION_ERROR"


class AuthenticationError(MemoryOSError):
    code = "AUTH_REQUIRED"


class OriginRejectedError(MemoryOSError):
    code = "ORIGIN_REJECTED"


class ProviderError(MemoryOSError):
    code = "PROVIDER_FAILURE"


class BackupError(MemoryOSError):
    code = "BACKUP_ERROR"


class TokenizerUnavailableError(MemoryOSError):
    code = "EXACT_TOKENIZER_REQUIRED"


class InsufficientBudgetError(MemoryOSError):
    code = "INSUFFICIENT_BUDGET"


class ContextChangedError(MemoryOSError):
    code = "CONTEXT_CHANGED"
