from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.phase0.registry import ApplicationRegistry
from app.phase0.service_manager import LifecycleRouter


@pytest.mark.asyncio
async def test_systemd_command_is_built_only_from_registered_unit(tmp_path: Path) -> None:
    registry_file = tmp_path / "registry.yaml"
    registry_file.write_text(
        "allowed_roots:\n"
        f"  - {tmp_path}\n"
        "applications:\n"
        "  - id: safe-app\n"
        "    name: Safe app\n"
        "    runtime:\n"
        "      type: systemd-user\n"
        "      unit: machinedeck-safe-app.service\n",
        encoding="utf-8",
    )
    registry = ApplicationRegistry.load(registry_file)
    manager = LifecycleRouter(registry).for_application("safe-app")
    mocked = AsyncMock(return_value=(0, "active"))
    with patch("app.phase0.service_manager._run", mocked):
        result = await manager.start("safe-app")
    assert result.succeeded
    mocked.assert_awaited_once_with(
        ["systemctl", "--user", "start", "machinedeck-safe-app.service"]
    )

