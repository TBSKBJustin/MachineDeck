from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


MAX_LOG_LINE_BYTES = 64 * 1024


class LogEvent(BaseModel):
    application_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: Literal["journal", "docker"]
    stream: Literal["stdout", "stderr", "unknown"] = "unknown"
    message: str
    service: str | None = None
    container_id: str | None = None
    cursor: str | None = None
    sequence: int | None = None
    truncated: bool = False
    original_size: int | None = None


class LogEnvelope(BaseModel):
    type: Literal["log", "status", "error", "warning", "eof"]
    data: dict


def bounded_message(message: str, max_bytes: int = MAX_LOG_LINE_BYTES) -> tuple[str, bool, int]:
    encoded = message.encode("utf-8", errors="replace")
    original_size = len(encoded)
    if original_size <= max_bytes:
        return message, False, original_size
    shortened = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return shortened, True, original_size
