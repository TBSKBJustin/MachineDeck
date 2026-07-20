from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


APP_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
UNIT_PATTERN = re.compile(r"^machinedeck-[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.service$")


class RegistryError(ValueError):
    pass


class ApplicationNotFoundError(RegistryError):
    pass


@dataclass(frozen=True)
class Application:
    id: str
    name: str
    runtime_type: str
    unit: str | None = None
    project_directory: Path | None = None
    compose_file: str | None = None


class ApplicationRegistry:
    """Read-only registry loaded from an operator-controlled YAML file."""

    def __init__(self, applications: dict[str, Application]) -> None:
        self._applications = applications

    @classmethod
    def load(cls, path: Path) -> "ApplicationRegistry":
        registry_path = path.resolve(strict=True)
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise RegistryError("Registry root must be a mapping")
        allowed_roots = cls._allowed_roots(raw.get("allowed_roots", []), registry_path.parent)
        applications: dict[str, Application] = {}
        entries = raw.get("applications", [])
        if not isinstance(entries, list):
            raise RegistryError("applications must be a list")
        for entry in entries:
            application = cls._parse_application(entry, registry_path.parent, allowed_roots)
            if application.id in applications:
                raise RegistryError(f"Duplicate application id: {application.id}")
            applications[application.id] = application
        return cls(applications)

    @staticmethod
    def _allowed_roots(values: Any, base: Path) -> tuple[Path, ...]:
        if not isinstance(values, list) or not values:
            raise RegistryError("At least one allowed_root is required")
        roots = []
        for value in values:
            if not isinstance(value, str):
                raise RegistryError("allowed_roots entries must be strings")
            candidate = Path(value)
            roots.append((base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve())
        return tuple(roots)

    @classmethod
    def _parse_application(
        cls, entry: Any, base: Path, allowed_roots: tuple[Path, ...]
    ) -> Application:
        if not isinstance(entry, dict):
            raise RegistryError("Application entries must be mappings")
        app_id = entry.get("id")
        name = entry.get("name")
        runtime = entry.get("runtime")
        if not isinstance(app_id, str) or not APP_ID_PATTERN.fullmatch(app_id):
            raise RegistryError(f"Invalid application id: {app_id!r}")
        if not isinstance(name, str) or not name.strip():
            raise RegistryError(f"Application {app_id} requires a name")
        if not isinstance(runtime, dict):
            raise RegistryError(f"Application {app_id} requires a runtime mapping")
        runtime_type = runtime.get("type")
        if runtime_type == "systemd-user":
            unit = runtime.get("unit")
            if not isinstance(unit, str) or not UNIT_PATTERN.fullmatch(unit):
                raise RegistryError(f"Application {app_id} has an invalid or unsafe unit")
            return Application(app_id, name, runtime_type, unit=unit)
        if runtime_type == "docker-compose":
            directory = runtime.get("project_directory")
            compose_file = runtime.get("compose_file", "compose.yaml")
            if not isinstance(directory, str) or not isinstance(compose_file, str):
                raise RegistryError(f"Application {app_id} has invalid Compose paths")
            if Path(compose_file).name != compose_file or compose_file in {".", ".."}:
                raise RegistryError(f"Application {app_id} has an unsafe compose_file")
            candidate = Path(directory)
            project = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
            if not any(project == root or project.is_relative_to(root) for root in allowed_roots):
                raise RegistryError(f"Application {app_id} project_directory is outside allowed roots")
            if not (project / compose_file).is_file():
                raise RegistryError(f"Application {app_id} Compose file does not exist")
            return Application(
                app_id,
                name,
                runtime_type,
                project_directory=project,
                compose_file=compose_file,
            )
        raise RegistryError(f"Application {app_id} has unsupported runtime type: {runtime_type!r}")

    def get(self, app_id: str) -> Application:
        if not isinstance(app_id, str) or not APP_ID_PATTERN.fullmatch(app_id):
            raise ApplicationNotFoundError("Application is not registered")
        try:
            return self._applications[app_id]
        except KeyError as exc:
            raise ApplicationNotFoundError(f"Application is not registered: {app_id}") from exc

    def list(self) -> list[Application]:
        return list(self._applications.values())

