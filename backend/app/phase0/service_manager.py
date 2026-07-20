from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .registry import ApplicationRegistry, RegistryError


@dataclass(frozen=True)
class LifecycleResult:
    application_id: str
    action: str
    succeeded: bool
    output: str


class ServiceManager(Protocol):
    async def start(self, app_id: str) -> LifecycleResult: ...
    async def stop(self, app_id: str) -> LifecycleResult: ...
    async def restart(self, app_id: str) -> LifecycleResult: ...
    async def status(self, app_id: str) -> LifecycleResult: ...
    async def logs(self, app_id: str, lines: int = 200) -> list[str]: ...


async def _run(command: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
    """Execute an internally constructed argument vector without a shell."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return 127, str(exc)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise
    output = (stdout + stderr).decode(errors="replace").strip()
    return process.returncode or 0, output


class UserSystemdServiceManager:
    def __init__(self, registry: ApplicationRegistry) -> None:
        self.registry = registry

    def _unit(self, app_id: str) -> str:
        application = self.registry.get(app_id)
        if application.runtime_type != "systemd-user" or application.unit is None:
            raise RegistryError(f"Application {app_id} is not a systemd-user application")
        return application.unit

    async def _action(self, app_id: str, action: str) -> LifecycleResult:
        unit = self._unit(app_id)
        return_code, output = await _run(["systemctl", "--user", action, unit])
        return LifecycleResult(app_id, action, return_code == 0, output)

    async def start(self, app_id: str) -> LifecycleResult:
        return await self._action(app_id, "start")

    async def stop(self, app_id: str) -> LifecycleResult:
        return await self._action(app_id, "stop")

    async def restart(self, app_id: str) -> LifecycleResult:
        return await self._action(app_id, "restart")

    async def status(self, app_id: str) -> LifecycleResult:
        return await self._action(app_id, "status")

    async def logs(self, app_id: str, lines: int = 200) -> list[str]:
        unit = self._unit(app_id)
        if not 1 <= lines <= 5000:
            raise ValueError("lines must be between 1 and 5000")
        _, output = await _run(
            ["journalctl", "--user-unit", unit, "--no-pager", "--lines", str(lines)]
        )
        return output.splitlines()


class DockerComposeServiceManager:
    def __init__(self, registry: ApplicationRegistry) -> None:
        self.registry = registry

    def _project(self, app_id: str) -> tuple[Path, str]:
        application = self.registry.get(app_id)
        if (
            application.runtime_type != "docker-compose"
            or application.project_directory is None
            or application.compose_file is None
        ):
            raise RegistryError(f"Application {app_id} is not a Docker Compose application")
        return application.project_directory, application.compose_file

    async def _action(self, app_id: str, action: str) -> LifecycleResult:
        project, compose_file = self._project(app_id)
        commands = {
            "start": ["docker", "compose", "-f", compose_file, "up", "-d"],
            "stop": ["docker", "compose", "-f", compose_file, "stop"],
            "restart": ["docker", "compose", "-f", compose_file, "restart"],
            "status": ["docker", "compose", "-f", compose_file, "ps", "--format", "json"],
        }
        return_code, output = await _run(commands[action], cwd=project)
        return LifecycleResult(app_id, action, return_code == 0, output)

    async def start(self, app_id: str) -> LifecycleResult:
        return await self._action(app_id, "start")

    async def stop(self, app_id: str) -> LifecycleResult:
        return await self._action(app_id, "stop")

    async def restart(self, app_id: str) -> LifecycleResult:
        return await self._action(app_id, "restart")

    async def status(self, app_id: str) -> LifecycleResult:
        return await self._action(app_id, "status")

    async def logs(self, app_id: str, lines: int = 200) -> list[str]:
        project, compose_file = self._project(app_id)
        if not 1 <= lines <= 5000:
            raise ValueError("lines must be between 1 and 5000")
        _, output = await _run(
            ["docker", "compose", "-f", compose_file, "logs", "--no-color", "--tail", str(lines)],
            cwd=project,
        )
        return output.splitlines()


class LifecycleRouter:
    """Select an adapter using trusted registry data, never request data."""

    def __init__(self, registry: ApplicationRegistry) -> None:
        self.registry = registry
        self.systemd = UserSystemdServiceManager(registry)
        self.compose = DockerComposeServiceManager(registry)

    def for_application(self, app_id: str) -> ServiceManager:
        application = self.registry.get(app_id)
        if application.runtime_type == "systemd-user":
            return self.systemd
        if application.runtime_type == "docker-compose":
            return self.compose
        raise RegistryError(f"No lifecycle adapter for {application.runtime_type}")
