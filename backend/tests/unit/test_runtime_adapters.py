from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.adapters.runtime import AdapterResult, DockerComposeAdapter, UserSystemdAdapter
from app.schemas.applications import ComposeRuntime, ProcessRuntime


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
    runtime = ProcessRuntime(type="process", working_dir=tmp_path, command=[str(executable), "app.py"])
    adapter = UserSystemdAdapter("safe-app", runtime)
    mocked = AsyncMock(return_value=AdapterResult(True))
    with patch("app.adapters.runtime.run_command", mocked):
        await adapter.restart()
    mocked.assert_awaited_once_with(
        ["systemctl", "--user", "restart", "machinedeck-safe-app.service"]
    )
