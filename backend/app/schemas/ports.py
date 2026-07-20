from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.applications import PortDefinition


class PortStatus(str, Enum):
    DECLARED = "DECLARED"
    LISTENING = "LISTENING"
    NOT_LISTENING = "NOT_LISTENING"
    UNDECLARED = "UNDECLARED"
    CONFLICTED = "CONFLICTED"
    UNKNOWN = "UNKNOWN"


class ObservedPort(BaseModel):
    bind_address: str
    host_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]
    pid: int | None = None
    process_name: str | None = None
    source: Literal["process", "compose", "system"]
    service: str | None = None
    container_id: str | None = None
    container_port: int | None = None
    application_id: str | None = None


class PortView(BaseModel):
    id: str
    name: str
    protocol: Literal["http", "https", "tcp", "udp"]
    declared: PortDefinition | None
    observed: ObservedPort | None
    status: PortStatus
    url: str | None = None


class PortsResponse(BaseModel):
    application_id: str
    ports: list[PortView]


class PortConflict(BaseModel):
    protocol: Literal["tcp", "udp"]
    bind_address: str
    port: int
    pid: int | None = None
    process_name: str | None = None
    application_id: str | None = None
    managed_by_machinedeck: bool = False


class EndpointsResponse(BaseModel):
    primary: PortView | None
    endpoints: list[PortView]


class SystemPortsResponse(BaseModel):
    ports: list[ObservedPort]
