from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.adapters.runtime import AdapterResult
from app.logging.adapters import (
    DockerComposeLogAdapter,
    JournalLogAdapter,
    LogSourceError,
    _bounded_lines,
)
from app.logging.models import MAX_LOG_LINE_BYTES
from app.schemas.applications import ApplicationManifest


FIXTURE_PROCESS = Path(__file__).resolve().parents[1] / "fixtures" / "process"
FIXTURE_COMPOSE = Path(__file__).resolve().parents[1] / "fixtures" / "compose"


class FakeProcess:
    def __init__(self, stdout: bytes = b"", return_code: int = 0, keep_open: bool = False) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.stdout.feed_data(stdout)
        if not keep_open:
            self.stdout.feed_eof()
        self.returncode = None if keep_open else return_code
        self.final_return_code = return_code
        self.waited = asyncio.Event()
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        if self.returncode is None:
            await self.waited.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.stdout.feed_eof()
        self.waited.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.waited.set()


def process_manifest() -> ApplicationManifest:
    return ApplicationManifest.model_validate(
        {
            "id": "log-process",
            "name": "Log Process",
            "runtime": {
                "type": "process",
                "working_dir": str(FIXTURE_PROCESS),
                "command": [str(FIXTURE_PROCESS / "example.sh")],
            },
        }
    )


def compose_manifest() -> ApplicationManifest:
    return ApplicationManifest.model_validate(
        {
            "id": "log-compose",
            "name": "Log Compose",
            "runtime": {
                "type": "compose",
                "working_dir": str(FIXTURE_COMPOSE),
                "compose_file": "compose.yaml",
                "project_name": "log-compose",
            },
        }
    )


def journal_line(message: str, cursor: str) -> bytes:
    return (
        json.dumps(
            {
                "__REALTIME_TIMESTAMP": "1784577600000000",
                "MESSAGE": message,
                "PRIORITY": "6",
                "__CURSOR": cursor,
            }
        ).encode()
        + b"\n"
    )


@pytest.mark.asyncio
async def test_journal_history_limit_and_cursor_follow_cleanup() -> None:
    history_process = FakeProcess(
        journal_line("one", "s=one")
        + journal_line("two", "s=two")
        + journal_line("three", "s=three")
    )
    follow_process = FakeProcess(journal_line("four", "s=four"), keep_open=True)
    commands: list[tuple[str, ...]] = []

    async def factory(*command: str, **_: object) -> FakeProcess:
        commands.append(command)
        return history_process if len(commands) == 1 else follow_process

    adapter = JournalLogAdapter(process_manifest(), process_factory=factory)
    with patch(
        "app.logging.adapters.run_command",
        AsyncMock(return_value=AdapterResult(True, "LoadState=loaded")),
    ):
        history = await adapter.history(limit=2)
        assert [event.message for event in history] == ["two", "three"]
        stream = adapter.follow(cursor=history[-1].cursor)
        followed = await stream.__anext__()
        assert followed.message == "four"
        await stream.aclose()
    assert "--after-cursor" in commands[1]
    assert "s=three" in commands[1]
    assert follow_process.terminated
    assert not adapter.active_processes


@pytest.mark.asyncio
async def test_missing_journal_unit_returns_structured_source_error() -> None:
    adapter = JournalLogAdapter(process_manifest(), process_factory=AsyncMock())
    with patch(
        "app.logging.adapters.run_command",
        AsyncMock(return_value=AdapterResult(False, "not found", 1)),
    ):
        with pytest.raises(LogSourceError, match="unavailable"):
            await adapter.history(limit=10)


@pytest.mark.asyncio
async def test_empty_history_succeeds() -> None:
    process = FakeProcess()
    adapter = JournalLogAdapter(
        process_manifest(), process_factory=AsyncMock(return_value=process)
    )
    with patch(
        "app.logging.adapters.run_command",
        AsyncMock(return_value=AdapterResult(True, "LoadState=loaded")),
    ):
        assert await adapter.history(limit=10) == []


@pytest.mark.asyncio
async def test_compose_history_maps_multiple_services_and_non_utf8() -> None:
    output = (
        b"api-1 | 2026-07-20T20:00:00Z API ready\n"
        b"worker-1 | 2026-07-20T20:00:01Z bad byte: \xff\n"
    )
    process = FakeProcess(output)
    ps_payload = json.dumps(
        [
            {"Name": "log-compose-api-1", "Service": "api", "ID": "aaa"},
            {"Name": "log-compose-worker-1", "Service": "worker", "ID": "bbb"},
        ]
    )
    adapter = DockerComposeLogAdapter(
        compose_manifest(), process_factory=AsyncMock(return_value=process)
    )
    with patch(
        "app.logging.adapters.run_command",
        AsyncMock(return_value=AdapterResult(True, ps_payload)),
    ):
        events = await adapter.history(limit=10)
    assert [(event.service, event.container_id) for event in events] == [
        ("api", "aaa"),
        ("worker", "bbb"),
    ]
    assert "�" in events[1].message
    assert events[0].timestamp.isoformat() == "2026-07-20T20:00:00+00:00"


@pytest.mark.asyncio
async def test_compose_follow_reconnects_after_container_recreation() -> None:
    first = FakeProcess(b"api-1 | 2026-07-20T20:00:00.123456789Z before recreate\n")
    second = FakeProcess(
        b"api-1 | 2026-07-20T20:00:01.123456789Z after recreate\n", keep_open=True
    )
    processes = [first, second]
    commands: list[tuple[str, ...]] = []

    async def factory(*command: str, **_: object) -> FakeProcess:
        commands.append(command)
        return processes.pop(0)

    ps_payload = json.dumps(
        [{"Name": "project-api-1", "Service": "api", "ID": "new-id", "State": "running"}]
    )
    adapter = DockerComposeLogAdapter(compose_manifest(), process_factory=factory)
    with patch(
        "app.logging.adapters.run_command",
        AsyncMock(return_value=AdapterResult(True, ps_payload)),
    ):
        stream = adapter.follow()
        before = await stream.__anext__()
        after = await asyncio.wait_for(stream.__anext__(), timeout=2)
        await stream.aclose()
    assert before.message == "before recreate"
    assert after.message == "after recreate"
    assert len(commands) == 2
    assert "--since" in commands[1]
    assert second.terminated


@pytest.mark.asyncio
async def test_oversized_line_is_bounded_and_marked() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * (MAX_LOG_LINE_BYTES + 500) + b"\n")
    reader.feed_eof()
    lines = [item async for item in _bounded_lines(reader)]
    raw, truncated, original_size = lines[0]
    assert len(raw) == MAX_LOG_LINE_BYTES
    assert truncated
    assert original_size == MAX_LOG_LINE_BYTES + 500
