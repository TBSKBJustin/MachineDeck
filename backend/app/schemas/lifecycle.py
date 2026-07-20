from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ApplicationStatus(str, Enum):
    STOPPED = "STOPPED"
    QUEUED = "QUEUED"
    CHECKING = "CHECKING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    UNHEALTHY = "UNHEALTHY"
    STOPPING = "STOPPING"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    DISABLED = "DISABLED"


class ExecutionStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ApplicationStateResponse(BaseModel):
    application_id: str
    instance_id: str
    status: ApplicationStatus
    runtime_identifier: str | None
    started_at: datetime | None
    stopped_at: datetime | None
    exit_code: int | None
    error_message: str | None
    updated_at: datetime


class LifecycleActionResponse(BaseModel):
    application_id: str
    execution_id: str
    action: str
    status: ApplicationStatus
    succeeded: bool
    error_code: str | None = None
    message: str = ""


class LogResponse(BaseModel):
    application_id: str
    source: str
    lines: list[str] = Field(default_factory=list)


class UnitConsistencyResponse(BaseModel):
    application_id: str
    unit_name: str | None
    status: str
    message: str = ""
