from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from memoryos.config import MemoryOSSettings
from memoryos.security.redaction import redact_secrets


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact_secrets(message, max_length=4000).text
        record.args = ()
        return True


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            exception = redact_secrets(self.formatException(record.exc_info), max_length=4000).text
            payload["exception"] = exception
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(settings: MemoryOSSettings) -> None:
    settings.ensure_directories()
    root = logging.getLogger("memoryos")
    target = (settings.log_dir / "memoryos.log").resolve()
    for existing in list(root.handlers):
        if isinstance(existing, RotatingFileHandler) and Path(existing.baseFilename) == target:
            return
        root.removeHandler(existing)
        existing.close()
    root.setLevel(settings.log_level.upper())
    handler = RotatingFileHandler(
        target,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(JsonLogFormatter())
    root.addHandler(handler)
