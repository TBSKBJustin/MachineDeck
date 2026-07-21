from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Freshness(str, Enum):
    LIVE = "LIVE"
    STALE = "STALE"
    OFFLINE = "OFFLINE"


class CollectorState(BaseModel):
    available: bool
    error_code: str | None = None
    message: str | None = None


class DiskMetrics(BaseModel):
    mountpoint: str
    filesystem: str | None = None
    total_bytes: int | None = Field(default=None, ge=0)
    used_bytes: int | None = Field(default=None, ge=0)
    free_bytes: int | None = Field(default=None, ge=0)
    percent: float | None = Field(default=None, ge=0, le=100)
    available: bool = True
    error: str | None = None


class HostMetrics(BaseModel):
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    cpu_per_core_percent: list[float] = Field(default_factory=list)
    load_average: tuple[float, float, float] | None = None
    memory_total_bytes: int | None = Field(default=None, ge=0)
    memory_used_bytes: int | None = Field(default=None, ge=0)
    memory_available_bytes: int | None = Field(default=None, ge=0)
    memory_percent: float | None = Field(default=None, ge=0, le=100)
    swap_total_bytes: int | None = Field(default=None, ge=0)
    swap_used_bytes: int | None = Field(default=None, ge=0)
    swap_percent: float | None = Field(default=None, ge=0, le=100)
    disks: list[DiskMetrics] = Field(default_factory=list)
    uptime_seconds: float | None = Field(default=None, ge=0)


class GpuProcessMetrics(BaseModel):
    pid: int = Field(ge=1)
    used_vram_bytes: int | None = Field(default=None, ge=0)
    process_name: str | None = None
    application_id: str | None = None
    managed: bool = False


class GpuMetrics(BaseModel):
    index: int = Field(ge=0)
    uuid: str
    name: str
    utilization_percent: float | None = Field(default=None, ge=0, le=100)
    memory_total_bytes: int = Field(ge=0)
    memory_used_bytes: int = Field(ge=0)
    memory_free_bytes: int = Field(ge=0)
    temperature_celsius: float | None = None
    power_usage_watts: float | None = Field(default=None, ge=0)
    power_limit_watts: float | None = Field(default=None, ge=0)
    fan_percent: float | None = Field(default=None, ge=0, le=100)
    process_count: int = Field(ge=0)
    processes: list[GpuProcessMetrics] = Field(default_factory=list)
    available: bool = True
    error: str | None = None


class ApplicationSummary(BaseModel):
    total: int = 0
    running: int = 0
    stopped: int = 0
    starting: int = 0
    stopping: int = 0
    unhealthy: int = 0
    failed: int = 0
    queued: int = 0
    unknown: int = 0
    disabled: int = 0


class DashboardSnapshot(BaseModel):
    collected_at: datetime
    collection_duration_ms: float = Field(ge=0)
    freshness: Freshness
    host: HostMetrics
    gpus: list[GpuMetrics]
    applications: ApplicationSummary
    collectors: dict[str, CollectorState]


class DashboardEnvelope(BaseModel):
    type: str = "dashboard_snapshot"
    data: DashboardSnapshot
