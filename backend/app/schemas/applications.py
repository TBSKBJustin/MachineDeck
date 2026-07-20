from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.schemas.lifecycle import ApplicationStatus


APP_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class ProcessRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["process"]
    working_dir: Path
    command: list[str] = Field(min_length=1, max_length=128)

    @field_validator("working_dir")
    @classmethod
    def absolute_working_directory(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("working_dir must be absolute")
        return value.resolve()

    @field_validator("command")
    @classmethod
    def safe_argument_vector(cls, value: list[str]) -> list[str]:
        if any(not argument or "\x00" in argument for argument in value):
            raise ValueError("command arguments must be non-empty and cannot contain null bytes")
        executable = Path(value[0])
        if not executable.is_absolute():
            raise ValueError("command executable must be an absolute path")
        return value


class ComposeRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["compose"]
    working_dir: Path
    compose_file: str = "compose.yaml"
    project_name: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]*$")

    @field_validator("working_dir")
    @classmethod
    def absolute_working_directory(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("working_dir must be absolute")
        return value.resolve()

    @field_validator("compose_file")
    @classmethod
    def safe_compose_filename(cls, value: str) -> str:
        if Path(value).name != value or value in {".", ".."}:
            raise ValueError("compose_file must be a filename without path components")
        if not value.endswith((".yaml", ".yml")):
            raise ValueError("compose_file must be a YAML file")
        return value


Runtime = Annotated[ProcessRuntime | ComposeRuntime, Field(discriminator="type")]


class PortDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=100)
    protocol: Literal["http", "https", "tcp", "udp"] = "http"
    host: int = Field(ge=1, le=65535)
    health_path: str | None = None

    @field_validator("health_path")
    @classmethod
    def validate_health_path(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("/"):
            raise ValueError("health_path must start with /")
        return value


class ApplicationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    enabled: bool = True
    runtime: Runtime
    environment: dict[str, str] = Field(default_factory=dict)
    ports: list[PortDefinition] = Field(default_factory=list, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("id")
    @classmethod
    def valid_application_id(cls, value: str) -> str:
        if not APP_ID_PATTERN.fullmatch(value):
            raise ValueError("id must contain lowercase letters, digits, and internal hyphens")
        return value

    @field_validator("environment")
    @classmethod
    def valid_environment_names(cls, value: dict[str, str]) -> dict[str, str]:
        invalid = [name for name in value if not ENVIRONMENT_NAME_PATTERN.fullmatch(name)]
        if invalid:
            raise ValueError(f"invalid environment variable names: {', '.join(invalid)}")
        return value

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", tag) for tag in value):
            raise ValueError("tags must contain lowercase letters, digits, and hyphens")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def unique_ports(self) -> "ApplicationManifest":
        ids = [port.id for port in self.ports]
        hosts = [port.host for port in self.ports]
        if len(ids) != len(set(ids)):
            raise ValueError("port ids must be unique")
        if len(hosts) != len(set(hosts)):
            raise ValueError("host ports must be unique within an application")
        return self


class ApplicationResponse(BaseModel):
    id: str
    name: str
    description: str
    runtime_type: str
    enabled: bool
    status: ApplicationStatus
    manifest: ApplicationManifest
    created_at: datetime
    updated_at: datetime


class ValidationIssue(BaseModel):
    field: str
    code: str
    message: str


class ValidationResponse(BaseModel):
    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)


def validate_manifest_paths(
    manifest: ApplicationManifest, allowed_roots: tuple[Path, ...] = settings.allowed_roots
) -> ValidationResponse:
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    working_dir = manifest.runtime.working_dir
    if not any(working_dir == root or working_dir.is_relative_to(root) for root in allowed_roots):
        errors.append(
            ValidationIssue(
                field="runtime.working_dir",
                code="PATH_NOT_ALLOWED",
                message="Working directory is outside configured allowed roots.",
            )
        )
    elif not working_dir.is_dir():
        errors.append(
            ValidationIssue(
                field="runtime.working_dir",
                code="WORKING_DIR_NOT_FOUND",
                message="Working directory does not exist.",
            )
        )
    if isinstance(manifest.runtime, ProcessRuntime):
        executable = Path(manifest.runtime.command[0]).resolve()
        if not any(executable == root or executable.is_relative_to(root) for root in allowed_roots):
            errors.append(
                ValidationIssue(
                    field="runtime.command[0]",
                    code="PATH_NOT_ALLOWED",
                    message="Executable is outside configured allowed roots.",
                )
            )
        elif not executable.is_file():
            errors.append(
                ValidationIssue(
                    field="runtime.command[0]",
                    code="EXECUTABLE_NOT_FOUND",
                    message="Executable does not exist.",
                )
            )
    else:
        compose_path = working_dir / manifest.runtime.compose_file
        if not compose_path.is_file():
            errors.append(
                ValidationIssue(
                    field="runtime.compose_file",
                    code="FILE_NOT_FOUND",
                    message="Compose file does not exist.",
                )
            )
    if manifest.environment:
        warnings.append(
            ValidationIssue(
                field="environment",
                code="SECRETS_NOT_CLASSIFIED",
                message="Environment values are stored as configuration; do not include unprotected secrets.",
            )
        )
    return ValidationResponse(valid=not errors, errors=errors, warnings=warnings)
