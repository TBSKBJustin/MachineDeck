from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import json

import pytest

from app.adapters.runtime import AdapterResult, DockerComposeAdapter, UserSystemdAdapter
from app.schemas.applications import ApplicationManifest, ComposeRuntime
from app.schemas.lifecycle import ApplicationStatus


@pytest.mark.asyncio
async def test_compose_adapter_constructs_fixed_argument_vector(tmp_path: Path) -> None:
    runtime = ComposeRuntime(
        type="compose", working_dir=tmp_path, compose_file="compose.yaml", project_name="safe-project"
    )
    adapter = DockerComposeAdapter("safe-app", runtime)
    mocked = AsyncMock(return_value=AdapterResult(True))
    with patch("app.adapters.runtime.run_command", mocked):
        await adapter.start()
        await adapter.stop()
    assert mocked.await_args_list[0].args == (
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "--project-name",
            "safe-project",
            "up",
            "-d",
        ],
    )
    assert mocked.await_args_list[0].kwargs == {"cwd": tmp_path}
    assert mocked.await_args_list[1].args == (
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "--project-name",
            "safe-project",
            "stop",
        ],
    )


@pytest.mark.asyncio
async def test_systemd_adapter_uses_only_deterministic_managed_unit(tmp_path: Path) -> None:
    executable = tmp_path / "python"
    manifest = ApplicationManifest.model_validate(
        {
            "id": "safe-app",
            "name": "Safe app",
            "runtime": {
                "type": "process",
                "working_dir": str(tmp_path),
                "command": [str(executable), "app.py"],
            },
        }
    )
    unit_manager = SimpleNamespace(
        install=AsyncMock(
            return_value=SimpleNamespace(
                rollback=AsyncMock(), previous_content=None, changed=False
            )
        )
    )
    adapter = UserSystemdAdapter(manifest, unit_manager)
    mocked = AsyncMock(
        side_effect=[
            AdapterResult(True),
            AdapterResult(True, "ActiveState=active\nSubState=running\nMainPID=123"),
        ]
    )
    with patch("app.adapters.runtime.run_command", mocked):
        await adapter.restart()
    assert mocked.await_args_list[0].args == (
        ["systemctl", "--user", "restart", "machinedeck-safe-app.service"],
    )


@pytest.mark.asyncio
async def test_systemd_activating_state_is_preserved(tmp_path: Path) -> None:
    manifest = ApplicationManifest.model_validate(
        {
            "id": "starting-app",
            "name": "Starting",
            "runtime": {
                "type": "process",
                "working_dir": str(tmp_path),
                "command": [str(tmp_path / "run")],
            },
        }
    )
    adapter = UserSystemdAdapter(manifest)
    mocked = AsyncMock(
        return_value=AdapterResult(True, "ActiveState=activating\nSubState=start\nMainPID=42")
    )
    with patch("app.adapters.runtime.run_command", mocked):
        state = await adapter.status()
    assert state.status == ApplicationStatus.STARTING


@pytest.mark.asyncio
async def test_compose_partial_failure_is_failed_state(tmp_path: Path) -> None:
    runtime = ComposeRuntime(type="compose", working_dir=tmp_path, compose_file="compose.yaml")
    adapter = DockerComposeAdapter("mixed-stack", runtime)
    payload = json.dumps(
        [
            {"Service": "web", "State": "running", "ExitCode": 0},
            {"Service": "worker", "State": "exited", "ExitCode": 1},
        ]
    )
    with patch(
        "app.adapters.runtime.run_command", AsyncMock(return_value=AdapterResult(True, payload))
    ):
        state = await adapter.status()
    assert state.status == ApplicationStatus.FAILED
