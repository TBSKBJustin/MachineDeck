from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.adapters.runtime import AdapterResult, UserSystemdAdapter
from app.schemas.applications import ApplicationManifest, validate_manifest_paths
from app.systemd.user_units import (
    UnitConsistency,
    UnitError,
    UserUnitManager,
    render_user_unit,
    unit_name_for,
    validate_rendered_unit,
)


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "process"
EXECUTABLE = FIXTURE_DIR / "example.sh"


def process_manifest(
    *,
    argument: str = "serve",
    environment: dict[str, str] | None = None,
) -> ApplicationManifest:
    return ApplicationManifest.model_validate(
        {
            "id": "safe-process",
            "name": "Safe Process",
            "runtime": {
                "type": "process",
                "working_dir": str(FIXTURE_DIR),
                "command": [str(EXECUTABLE), argument],
            },
            "environment": environment or {},
        }
    )


@pytest.mark.parametrize("application_id", ["has space", "slash/name", "line\nbreak", "semi;colon"])
def test_unsafe_unit_ids_are_rejected(application_id: str) -> None:
    with pytest.raises(UnitError):
        unit_name_for(application_id)


def test_arguments_are_quoted_without_shell_or_directive_injection() -> None:
    content = render_user_unit(process_manifest(argument='value"; $(touch /tmp/nope); --flag'))
    assert content.count("ExecStart=") == 1
    assert "ExecStop=" not in content
    assert "$$(touch /tmp/nope)" in content
    assert '\\";' in content


def test_actual_newline_in_command_argument_is_rejected() -> None:
    with pytest.raises(UnitError, match="newlines"):
        render_user_unit(process_manifest(argument="safe\nExecStop=/bin/false"))


def test_environment_newline_is_rejected_by_manifest_schema() -> None:
    with pytest.raises(ValidationError, match="cannot contain newlines"):
        process_manifest(environment={"TOKEN": "first\nExecStop=/bin/false"})


def test_environment_quotes_percent_and_dollars_are_data() -> None:
    content = render_user_unit(
        process_manifest(environment={"SAFE_VALUE": 'quote" 100% $HOME ; still-data'})
    )
    assert 'Environment="SAFE_VALUE=quote\\" 100%% $HOME ; still-data"' in content


def test_working_directory_uses_systemd_path_escaping() -> None:
    content = render_user_unit(process_manifest())
    working_directory = next(
        line for line in content.splitlines() if line.startswith("WorkingDirectory=")
    )
    assert not working_directory.startswith('WorkingDirectory="')


def test_rendered_unit_rejects_unknown_directive() -> None:
    content = render_user_unit(process_manifest())
    with pytest.raises(UnitError, match="unsupported directive"):
        validate_rendered_unit(content.replace("Restart=no", "Restart=no\nExecStop=/bin/false"))


def test_symlinked_working_directory_cannot_escape_allowed_root(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    executable = outside / "run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    link = allowed / "linked-project"
    link.symlink_to(outside, target_is_directory=True)
    manifest = ApplicationManifest.model_validate(
        {
            "id": "linked-app",
            "name": "Linked",
            "runtime": {
                "type": "process",
                "working_dir": str(link),
                "command": [str(executable)],
            },
        }
    )
    with patch(
        "app.systemd.user_units.validate_manifest_paths",
        side_effect=lambda item: validate_manifest_paths(item, allowed_roots=(allowed,)),
    ):
        with pytest.raises(UnitError, match="outside configured allowed roots"):
            render_user_unit(manifest)


def test_symlinked_unit_directory_and_target_are_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    real.chmod(0o700)
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real, target_is_directory=True)
    with pytest.raises(UnitError, match="not a symlink"):
        UserUnitManager(linked_directory).target_path("safe-process")

    target = real / "machinedeck-safe-process.service"
    target.symlink_to(tmp_path / "outside.service")
    with pytest.raises(UnitError, match="not a symlink"):
        UserUnitManager(real).target_path("safe-process")


def test_world_writable_unit_directory_is_rejected(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir(mode=0o777)
    unit_dir.chmod(0o777)
    with pytest.raises(UnitError, match="world-writable"):
        UserUnitManager(unit_dir).target_path("safe-process")


@pytest.mark.asyncio
async def test_daemon_reload_failure_restores_previous_unit(tmp_path: Path) -> None:
    manager = UserUnitManager(tmp_path)
    target = manager.target_path("safe-process", create_directory=True)
    previous = b"old unit content\n"
    target.write_bytes(previous)
    target.chmod(0o644)
    runner = AsyncMock(
        side_effect=[
            AdapterResult(True),
            AdapterResult(False, "reload failed"),
            AdapterResult(True),
        ]
    )
    with pytest.raises(UnitError, match="reload failed"):
        await manager.install(process_manifest(), runner)
    assert target.read_bytes() == previous
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.asyncio
async def test_start_failure_rolls_back_updated_unit(tmp_path: Path) -> None:
    manager = UserUnitManager(tmp_path)
    target = manager.target_path("safe-process", create_directory=True)
    previous = b"previous valid unit\n"
    target.write_bytes(previous)
    target.chmod(0o644)
    runner = AsyncMock(
        side_effect=[
            AdapterResult(True),  # systemd-analyze verify
            AdapterResult(True),  # daemon-reload after install
            AdapterResult(False, "start failed", 1, "RUNTIME_COMMAND_FAILED"),
            AdapterResult(True),  # daemon-reload after rollback
        ]
    )
    adapter = UserSystemdAdapter(process_manifest(), manager)
    with patch("app.adapters.runtime.run_command", runner):
        result = await adapter.start()
    assert not result.succeeded
    assert target.read_bytes() == previous


def test_consistency_detects_missing_match_and_mismatch(tmp_path: Path) -> None:
    manager = UserUnitManager(tmp_path)
    manifest = process_manifest()
    missing = manager.consistency(manifest)
    assert missing.status == UnitConsistency.MISSING

    target = manager.target_path(manifest.id, create_directory=True)
    target.write_text(render_user_unit(manifest), encoding="utf-8")
    target.chmod(0o644)
    assert manager.consistency(manifest).status == UnitConsistency.MATCH
    target.write_text("externally modified\n", encoding="utf-8")
    target.chmod(0o644)
    assert manager.consistency(manifest).status == UnitConsistency.MISMATCH
