from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from app.adapters.runtime import run_command
from app.logging.models import MAX_LOG_LINE_BYTES, LogEvent, bounded_message
from app.schemas.applications import ApplicationManifest, ComposeRuntime, ProcessRuntime
from app.systemd.user_units import unit_name_for


class LogSourceError(RuntimeError):
    pass


class RuntimeLogAdapter(Protocol):
    async def history(
        self,
        *,
        limit: int,
        since: datetime | None = None,
        cursor: str | None = None,
    ) -> list[LogEvent]: ...

    def follow(self, *, cursor: str | None = None) -> AsyncIterator[LogEvent]: ...


ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]


def _safe_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if not cursor or len(cursor) > 2048 or any(char in cursor for char in ("\n", "\r", "\x00")):
        raise LogSourceError("Invalid journal cursor")
    return cursor


async def _bounded_lines(
    stream: asyncio.StreamReader, max_bytes: int = MAX_LOG_LINE_BYTES
) -> AsyncIterator[tuple[bytes, bool, int]]:
    prefix = bytearray()
    original_size = 0
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            if original_size:
                yield bytes(prefix), truncated, original_size
            return
        for byte in chunk:
            if byte == 10:
                yield bytes(prefix), truncated, original_size
                prefix.clear()
                original_size = 0
                truncated = False
                continue
            original_size += 1
            if len(prefix) < max_bytes:
                prefix.append(byte)
            else:
                truncated = True


class SubprocessLogAdapter:
    def __init__(self, process_factory: ProcessFactory = asyncio.create_subprocess_exec) -> None:
        self.process_factory = process_factory
        self.active_processes: set[asyncio.subprocess.Process] = set()

    async def _stream_command(
        self, command: list[str], cwd: Path | None = None
    ) -> AsyncIterator[tuple[bytes, bool, int]]:
        try:
            process = await self.process_factory(
                *command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise LogSourceError(str(exc)) from exc
        self.active_processes.add(process)
        stderr_task = asyncio.create_task(process.stderr.read(64 * 1024))
        try:
            async for line in _bounded_lines(process.stdout):
                yield line
            return_code = await process.wait()
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            if return_code != 0:
                raise LogSourceError(stderr or f"Log reader exited with status {return_code}")
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if not stderr_task.done():
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
            self.active_processes.discard(process)


class JournalLogAdapter(SubprocessLogAdapter):
    def __init__(
        self,
        manifest: ApplicationManifest,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
    ) -> None:
        if not isinstance(manifest.runtime, ProcessRuntime):
            raise ValueError("JournalLogAdapter requires a process manifest")
        super().__init__(process_factory)
        self.manifest = manifest
        self.unit = unit_name_for(manifest.id)

    async def _ensure_unit(self) -> None:
        result = await run_command(
            [
                "systemctl",
                "--user",
                "show",
                self.unit,
                "--property=LoadState",
                "--no-pager",
            ]
        )
        if not result.succeeded or "LoadState=not-found" in result.message:
            raise LogSourceError(f"Managed unit is unavailable: {self.unit}")

    def _parse(self, raw: bytes, truncated: bool, original_size: int) -> LogEvent:
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            timestamp_value = int(payload.get("__REALTIME_TIMESTAMP", 0))
            timestamp = (
                datetime.fromtimestamp(timestamp_value / 1_000_000, tz=timezone.utc)
                if timestamp_value
                else datetime.now(timezone.utc)
            )
            message_value = payload.get("MESSAGE", "")
            message = message_value if isinstance(message_value, str) else str(message_value)
            priority = int(payload.get("PRIORITY", 6))
            stream = "stderr" if priority <= 3 else "unknown"
            message, message_truncated, message_size = bounded_message(message)
            return LogEvent(
                application_id=self.manifest.id,
                timestamp=timestamp,
                source="journal",
                stream=stream,
                message=message,
                cursor=payload.get("__CURSOR"),
                truncated=truncated or message_truncated,
                original_size=original_size if truncated else message_size,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return LogEvent(
                application_id=self.manifest.id,
                source="journal",
                message=raw.decode("utf-8", errors="replace"),
                truncated=truncated,
                original_size=original_size,
            )

    async def history(
        self,
        *,
        limit: int,
        since: datetime | None = None,
        cursor: str | None = None,
    ) -> list[LogEvent]:
        await self._ensure_unit()
        command = [
            "journalctl",
            "--user",
            "--unit",
            self.unit,
            "--output=json",
            "--no-pager",
            "--lines",
            str(limit),
        ]
        if since is not None:
            command.extend(["--since", since.astimezone(timezone.utc).isoformat()])
        if _safe_cursor(cursor) is not None:
            command.extend(["--after-cursor", cursor])
        events: deque[LogEvent] = deque(maxlen=limit)
        async for raw, truncated, original_size in self._stream_command(command):
            if raw:
                events.append(self._parse(raw, truncated, original_size))
        return list(events)

    async def follow(self, *, cursor: str | None = None) -> AsyncIterator[LogEvent]:
        await self._ensure_unit()
        command = [
            "journalctl",
            "--user",
            "--unit",
            self.unit,
            "--output=json",
            "--follow",
            "--lines=0",
        ]
        if _safe_cursor(cursor) is not None:
            command.extend(["--after-cursor", cursor])
        async with aclosing(self._stream_command(command)) as stream:
            async for raw, truncated, original_size in stream:
                if raw:
                    yield self._parse(raw, truncated, original_size)


COMPOSE_LINE = re.compile(
    r"^(?P<prefix>[^|]+?)\s*\|\s*(?:(?P<timestamp>\d{4}-\d{2}-\d{2}T\S+)\s+)?(?P<message>.*)$"
)


def _parse_compose_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    # Python 3.10 accepts microseconds while Docker commonly emits nanoseconds.
    normalized = re.sub(r"(\.\d{6})\d+(?=Z|[+-])", r"\1", value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.now(timezone.utc)


class DockerComposeLogAdapter(SubprocessLogAdapter):
    def __init__(
        self,
        manifest: ApplicationManifest,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
    ) -> None:
        if not isinstance(manifest.runtime, ComposeRuntime):
            raise ValueError("DockerComposeLogAdapter requires a Compose manifest")
        super().__init__(process_factory)
        self.manifest = manifest
        self.runtime = manifest.runtime
        self.base_command = ["docker", "compose", "-f", self.runtime.compose_file]
        if self.runtime.project_name:
            self.base_command.extend(["--project-name", self.runtime.project_name])
        self.container_services: dict[str, tuple[str, str | None]] = {}
        self.has_running_containers = False

    async def _refresh_services(self) -> bool:
        result = await run_command(
            self.base_command + ["ps", "--all", "--format", "json"], cwd=self.runtime.working_dir
        )
        if not result.succeeded or not result.message:
            self.container_services = {}
            self.has_running_containers = False
            return False
        try:
            payload = json.loads(result.message)
            containers = payload if isinstance(payload, list) else [payload]
        except json.JSONDecodeError:
            try:
                containers = [json.loads(line) for line in result.message.splitlines() if line]
            except json.JSONDecodeError:
                return False
        aliases: dict[str, tuple[str, str | None]] = {}
        for item in containers:
            name = str(item.get("Name") or item.get("Names") or "")
            service = str(item.get("Service") or "unknown")
            container_id = str(item.get("ID")) if item.get("ID") else None
            if name:
                aliases[name] = (service, container_id)
                suffix = re.search(rf"(?:^|-){re.escape(service)}-(\d+)$", name)
                if suffix:
                    aliases[f"{service}-{suffix.group(1)}"] = (service, container_id)
            aliases.setdefault(service, (service, container_id))
        self.container_services = aliases
        self.has_running_containers = any(
            str(item.get("State", "")).lower() == "running"
            or str(item.get("Status", "")).lower().startswith("up")
            for item in containers
        )
        return self.has_running_containers

    def _parse(self, raw: bytes, truncated: bool, original_size: int) -> LogEvent:
        decoded = raw.decode("utf-8", errors="replace")
        match = COMPOSE_LINE.match(decoded)
        if match:
            container_name = match.group("prefix").strip()
            service, container_id = self.container_services.get(
                container_name, (container_name, None)
            )
            timestamp_text = match.group("timestamp")
            timestamp = _parse_compose_timestamp(timestamp_text)
            message = match.group("message")
        else:
            service = None
            container_id = None
            timestamp = datetime.now(timezone.utc)
            message = decoded
        message, message_truncated, message_size = bounded_message(message)
        return LogEvent(
            application_id=self.manifest.id,
            timestamp=timestamp,
            source="docker",
            message=message,
            service=service,
            container_id=container_id,
            truncated=truncated or message_truncated,
            original_size=original_size if truncated else message_size,
        )

    async def history(
        self,
        *,
        limit: int,
        since: datetime | None = None,
        cursor: str | None = None,
    ) -> list[LogEvent]:
        await self._refresh_services()
        command = self.base_command + ["logs", "--timestamps", "--no-color", "--tail", str(limit)]
        if since is not None:
            command.extend(["--since", since.astimezone(timezone.utc).isoformat()])
        events: deque[LogEvent] = deque(maxlen=limit)
        async for raw, truncated, original_size in self._stream_command(
            command, cwd=self.runtime.working_dir
        ):
            if raw:
                events.append(self._parse(raw, truncated, original_size))
        return list(events)

    async def follow(self, *, cursor: str | None = None) -> AsyncIterator[LogEvent]:
        await self._refresh_services()
        last_timestamp: datetime | None = None
        first_attempt = True
        while first_attempt or self.has_running_containers:
            first_attempt = False
            command = self.base_command + [
                "logs",
                "--follow",
                "--timestamps",
                "--no-color",
                "--tail",
                "0",
            ]
            if last_timestamp is not None:
                command.extend(["--since", last_timestamp.astimezone(timezone.utc).isoformat()])
            async with aclosing(
                self._stream_command(command, cwd=self.runtime.working_dir)
            ) as stream:
                async for raw, truncated, original_size in stream:
                    if raw:
                        event = self._parse(raw, truncated, original_size)
                        known_services = {value[0] for value in self.container_services.values()}
                        if event.service not in known_services:
                            # Container recreation can change the prefix; refresh metadata without
                            # changing the trusted follow command.
                            await self._refresh_services()
                            event = self._parse(raw, truncated, original_size)
                        last_timestamp = event.timestamp
                        yield event
            if not await self._refresh_services():
                return
            await asyncio.sleep(0.5)


def log_adapter_for(manifest: ApplicationManifest) -> RuntimeLogAdapter:
    if isinstance(manifest.runtime, ProcessRuntime):
        return JournalLogAdapter(manifest)
    if isinstance(manifest.runtime, ComposeRuntime):
        return DockerComposeLogAdapter(manifest)
    raise ValueError(f"Unsupported log runtime: {manifest.runtime.type}")
