from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class AuditCategory(str, Enum):
    AUTHENTICATION = "authentication"
    APPLICATION_REGISTRY = "application.registry"
    APPLICATION_LIFECYCLE = "application.lifecycle"
    SYSTEM = "system"
    OTHER = "other"


class AuditActor(BaseModel):
    type: Literal["administrator", "system", "anonymous", "unknown"]
    id: str


class AuditTarget(BaseModel):
    type: str
    id: str
    name: str | None = None


class AuditRequest(BaseModel):
    method: str | None = None
    path: str | None = None


class AuditExecutionLink(BaseModel):
    id: str
    action: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None


class AuditEventResponse(BaseModel):
    id: str
    timestamp: datetime
    actor: AuditActor
    category: AuditCategory
    action: str
    raw_action: str
    result: str
    target: AuditTarget
    execution_id: str | None = None
    execution: AuditExecutionLink | None = None
    request: AuditRequest | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AuditEventPage(BaseModel):
    events: list[AuditEventResponse]
    next_cursor: str | None = None
    has_more: bool = False
