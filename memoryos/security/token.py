from __future__ import annotations

import hmac
import os
import secrets
from contextlib import suppress
from pathlib import Path


class TokenManager:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get_or_create(self) -> str:
        if self.path.exists():
            token = self.path.read_text(encoding="utf-8").strip()
            if token:
                return token
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(48)
        self.path.write_text(token, encoding="utf-8")
        self._restrict_permissions()
        return token

    def verify(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(candidate, self.get_or_create())

    def _restrict_permissions(self) -> None:
        with suppress(OSError):
            os.chmod(self.path, 0o600)
