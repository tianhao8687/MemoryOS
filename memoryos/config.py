from __future__ import annotations

import os
import sys
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from platformdirs import user_data_path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    override = os.environ.get("MEMORYOS_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path(user_data_path("MemoryOS", appauthor=False)).resolve()


class MemoryOSSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMORYOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_assignment=True,
    )

    data_dir: Path = Field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "INFO"
    busy_timeout_ms: int = 5000
    source_excerpt_limit: int = 2000
    allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1",
            "http://localhost",
            "http://127.0.0.1:8765",
            "http://localhost:8765",
        ]
    )
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    extractor_base_url: str | None = None
    extractor_model: str | None = None
    extractor_api_key: str | None = None
    provider_timeout_seconds: float = 20.0
    provider_max_input_chars: int = 12000
    relationship_model: str | None = None
    reranker_model: str | None = None
    consolidation_model: str | None = None
    staleness_model: str | None = None

    @field_validator("host")
    @classmethod
    def require_loopback_default(cls, value: str) -> str:
        if value.lower() == "localhost":
            return value
        try:
            loopback = ip_address(value).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("only loopback bind addresses are permitted")
        return value

    @field_validator("data_dir", mode="before")
    @classmethod
    def normalize_data_dir(cls, value: object) -> Path:
        return Path(str(value)).expanduser().resolve()

    @property
    def database_path(self) -> Path:
        return self.data_dir / "memoryos.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "auth.token"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def runtime_path(self) -> Path:
        return self.data_dir / "runtime.json"

    @property
    def web_dist(self) -> Path:
        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            return Path(str(frozen_root)) / "web_dist"
        return Path(__file__).resolve().parents[1] / "web" / "dist"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def settings_for(data_dir: Path | str | None = None, **overrides: Any) -> MemoryOSSettings:
    if data_dir is not None:
        overrides["data_dir"] = Path(data_dir)
    return MemoryOSSettings(**overrides)
