from __future__ import annotations

import os
import sys
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from platformdirs import user_data_path
from pydantic import Field, field_validator, model_validator
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
    port: int = Field(default=8765, ge=1, le=65535)
    log_level: str = "INFO"
    busy_timeout_ms: int = Field(default=5000, ge=1, le=300_000)
    source_excerpt_limit: int = Field(default=2000, ge=100, le=100_000)
    allowed_origins: list[str] = Field(default_factory=list)
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    extractor_base_url: str | None = None
    extractor_model: str | None = None
    extractor_api_key: str | None = None
    provider_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    provider_max_input_chars: int = Field(default=12000, ge=1, le=1_000_000)
    relationship_model: str | None = None
    reranker_model: str | None = None
    consolidation_model: str | None = None
    ann_enabled: bool = True
    context_compiler_mode: str = "legacy"
    context_budget_tiny_tokens: int = Field(default=384, ge=64, le=50000)
    context_budget_small_tokens: int = Field(default=768, ge=64, le=50000)
    context_budget_medium_tokens: int = Field(default=1536, ge=64, le=50000)
    context_budget_large_tokens: int = Field(default=3072, ge=64, le=50000)
    context_snapshot_ttl_seconds: int = Field(default=604_800, ge=60, le=31_536_000)
    context_snapshot_cleanup_batch_size: int = Field(default=100, ge=1, le=10_000)
    context_delta_fallback_ratio: float = Field(default=0.8, gt=0, le=1)
    mcp_tool_profile: str = "all"

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

    @field_validator("allowed_origins")
    @classmethod
    def require_explicit_loopback_origins(cls, values: list[str]) -> list[str]:
        for value in values:
            parsed = urlsplit(value)
            hostname = parsed.hostname or ""
            try:
                loopback = hostname.lower() == "localhost" or ip_address(hostname).is_loopback
                port = parsed.port
            except ValueError:
                loopback = False
                port = None
            if (
                parsed.scheme not in {"http", "https"}
                or not loopback
                or port is None
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("allowed origins must be explicit loopback URLs with a port")
        return values

    @field_validator("log_level")
    @classmethod
    def require_known_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @field_validator("context_compiler_mode")
    @classmethod
    def require_known_context_compiler_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"legacy", "msc_shadow", "msc"}:
            raise ValueError("context compiler mode must be legacy, msc_shadow, or msc")
        return normalized

    @field_validator("mcp_tool_profile")
    @classmethod
    def require_known_tool_profile(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"all", "core", "governance", "debug"}:
            raise ValueError("MCP tool profile must be all, core, governance, or debug")
        return normalized

    @model_validator(mode="after")
    def require_monotonic_context_budget_profiles(self) -> MemoryOSSettings:
        profiles = (
            self.context_budget_tiny_tokens,
            self.context_budget_small_tokens,
            self.context_budget_medium_tokens,
            self.context_budget_large_tokens,
        )
        if profiles != tuple(sorted(profiles)):
            raise ValueError("context token budget profiles must be monotonic from tiny to large")
        return self

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
    def ann_dir(self) -> Path:
        return self.data_dir / "vector-indexes"

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
        self.ann_dir.mkdir(parents=True, exist_ok=True)


def settings_for(data_dir: Path | str | None = None, **overrides: Any) -> MemoryOSSettings:
    if data_dir is not None:
        overrides["data_dir"] = Path(data_dir)
    return MemoryOSSettings(**overrides)
