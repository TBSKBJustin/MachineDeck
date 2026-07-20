from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.schemas.applications import ApplicationManifest, ComposeRuntime, ProcessRuntime
from app.schemas.lifecycle import ApplicationStatus
from app.systemd.user_units import UnitError, UserUnitManager


@dataclass(frozen=True)
class AdapterResult:
    succeeded: bool
    message: str = ""
    exit_code: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class RuntimeState:
    status: ApplicationStatus
    runtime_identifier: str
    metadata: dict = field(default_factory=dict)
    error_message: str | None = None


class RuntimeAdapter(Protocol):
    async def start(self) -> AdapterResult: ...
    async def stop(self) -> AdapterResult: ...
    async def restart(self) -> AdapterResult: ...
    async def status(self) -> RuntimeState: ...
    async def logs(self, lines: int) -> list[str]: ...


async def run_command(
    command: list[str], cwd: Path | None = None, timeout: int = 120
) -> AdapterResult:
    """Run only adapter-constructed argument vectors; a shell is never used."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return AdapterResult(False, str(exc), error_code="RUNTIME_UNAVAILABLE")
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return AdapterResult(False, "Runtime command timed out", error_code="RUNTIME_TIMEOUT")
    message = (stdout + stderr).decode(errors="replace").strip()
    return AdapterResult(
        process.returncode == 0,
        message,
        exit_code=process.returncode,
        error_code=None if process.returncode == 0 else "RUNTIME_COMMAND_FAILED",
    )


class UserSystemdAdapter:
    def __init__(
        self, manifest: ApplicationManifest, unit_manager: UserUnitManager | None = None
    ) -> None:
        if not isinstance(manifest.runtime, ProcessRuntime):
            raise ValueError("UserSystemdAdapter requires a process manifest")
        self.manifest = manifest
        self.application_id = manifest.id
        self.runtime = manifest.runtime
        self.unit = f"machinedeck-{manifest.id}.service"
        self.unit_manager = unit_manager or UserUnitManager()

    async def _action(self, action: str) -> AdapterResult:
        return await run_command(["systemctl", "--user", action, self.unit])

    async def start(self) -> AdapterResult:
        return await self._install_and_action("start")

    async def stop(self) -> AdapterResult:
        return await self._action("stop")

    async def restart(self) -> AdapterResult:
        return await self._install_and_action("restart")

    async def _install_and_action(self, action: str) -> AdapterResult:
        try:
            receipt = await self.unit_manager.install(self.manifest, run_command)
        except UnitError as exc:
            return AdapterResult(False, str(exc), error_code="UNIT_INSTALL_FAILED")
        result = await self._action(action)
        if result.succeeded:
            confirmed = await self.status()
            if confirmed.status in {ApplicationStatus.RUNNING, ApplicationStatus.STARTING}:
                return result
            result = AdapterResult(
                False,
                confirmed.error_message or f"Unit entered {confirmed.status.value} after {action}",
                result.exit_code,
                "UNIT_START_FAILED",
            )
        await receipt.rollback(run_command)
        if action == "restart" and receipt.changed and receipt.previous_content is not None:
            await self._action("restart")
        return result

    async def status(self) -> RuntimeState:
        result = await run_command(
            [
                "systemctl",
                "--user",
                "show",
                self.unit,
                "--property=ActiveState,SubState,MainPID",
                "--no-pager",
            ]
        )
        if not result.succeeded:
            return RuntimeState(
                ApplicationStatus.UNKNOWN,
                self.unit,
                error_message=result.message or "Unable to read systemd state",
            )
        values = {}
        for line in result.message.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        active = values.get("ActiveState")
        if active == "active":
            status = ApplicationStatus.RUNNING
        elif active == "deactivating":
            status = ApplicationStatus.STOPPING
        elif active == "activating":
            status = ApplicationStatus.STARTING
        elif active == "inactive":
            status = ApplicationStatus.STOPPED
        elif active == "failed":
            status = ApplicationStatus.FAILED
        else:
            status = ApplicationStatus.UNKNOWN
        return RuntimeState(status, self.unit, metadata=values)

    async def logs(self, lines: int) -> list[str]:
        result = await run_command(
            ["journalctl", "--user-unit", self.unit, "--no-pager", "--lines", str(lines)]
        )
        if not result.succeeded:
            raise RuntimeError(result.message or "Unable to read journal logs")
        return result.message.splitlines()


class DockerComposeAdapter:
    def __init__(self, application_id: str, runtime: ComposeRuntime) -> None:
        self.application_id = application_id
        self.runtime = runtime
        self.identifier = runtime.project_name or runtime.working_dir.name
        self.base_command = ["docker", "compose", "-f", runtime.compose_file]
        if runtime.project_name:
            self.base_command.extend(["--project-name", runtime.project_name])

    async def _action(self, arguments: list[str]) -> AdapterResult:
        return await run_command(self.base_command + arguments, cwd=self.runtime.working_dir)

    async def start(self) -> AdapterResult:
        return await self._action(["up", "-d"])

    async def stop(self) -> AdapterResult:
        return await self._action(["stop"])

    async def restart(self) -> AdapterResult:
        return await self._action(["restart"])

    async def status(self) -> RuntimeState:
        result = await self._action(["ps", "--format", "json"])
        if not result.succeeded:
            return RuntimeState(
                ApplicationStatus.UNKNOWN,
                self.identifier,
                error_message=result.message or "Unable to read Compose state",
            )
        if not result.message:
            return RuntimeState(ApplicationStatus.STOPPED, self.identifier)
        try:
            payload = json.loads(result.message)
            containers = payload if isinstance(payload, list) else [payload]
        except json.JSONDecodeError:
            try:
                containers = [json.loads(line) for line in result.message.splitlines() if line]
            except json.JSONDecodeError:
                return RuntimeState(
                    ApplicationStatus.UNKNOWN,
                    self.identifier,
                    error_message="Docker Compose returned invalid JSON",
                )
        states = {str(container.get("State", "")).lower() for container in containers}
        health = {str(container.get("Health", "")).lower() for container in containers}
        failed = any(
            str(container.get("State", "")).lower() in {"dead", "failed"}
            or int(container.get("ExitCode") or 0) != 0
            for container in containers
        )
        if failed:
            status = ApplicationStatus.FAILED
        elif "unhealthy" in health:
            status = ApplicationStatus.UNHEALTHY
        elif states and states <= {"running"}:
            status = ApplicationStatus.RUNNING
        elif not states or states <= {"exited", "created", "stopped"}:
            status = ApplicationStatus.STOPPED
        else:
            status = ApplicationStatus.UNKNOWN
        return RuntimeState(status, self.identifier, metadata={"containers": containers})

    async def logs(self, lines: int) -> list[str]:
        result = await self._action(["logs", "--no-color", "--tail", str(lines)])
        if not result.succeeded:
            raise RuntimeError(result.message or "Unable to read Docker logs")
        return result.message.splitlines()


def adapter_for(manifest: ApplicationManifest) -> RuntimeAdapter:
    if isinstance(manifest.runtime, ProcessRuntime):
        return UserSystemdAdapter(manifest)
    if isinstance(manifest.runtime, ComposeRuntime):
        return DockerComposeAdapter(manifest.id, manifest.runtime)
    raise ValueError(f"Unsupported runtime: {manifest.runtime.type}")
