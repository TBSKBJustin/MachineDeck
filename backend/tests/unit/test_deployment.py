from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def load_deploy_module() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "machinedeck_deploy.py"
    spec = importlib.util.spec_from_file_location("machinedeck_deploy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through the registered module.
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


deploy_module = load_deploy_module()


class FakeRunner:
    def __init__(self, database: Path, *, active: bool = False, fail_on: str | None = None) -> None:
        self.database = database
        self.active = active
        self.fail_on = fail_on
        self.commands: list[list[str]] = []

    def run(
        self,
        arguments: list[str | Path],
        *,
        check: bool = True,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(argument) for argument in arguments]
        self.commands.append(command)
        joined = " ".join(command)
        if self.fail_on and self.fail_on in joined:
            raise subprocess.CalledProcessError(1, command)
        if "is-active" in command:
            return subprocess.CompletedProcess(command, 0 if self.active else 3, "active\n" if self.active else "inactive\n", "")
        if command[1:3] == ["-m", "venv"]:
            binary_dir = Path(command[-1]) / "bin"
            binary_dir.mkdir(parents=True)
            (binary_dir / "python").touch(mode=0o755)
            (binary_dir / "alembic").touch(mode=0o755)
        if command and command[0].endswith("/alembic"):
            self.database.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.database) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS deployment_test (value TEXT)")
                connection.execute("INSERT INTO deployment_test VALUES ('migrated')")
        return subprocess.CompletedProcess(command, 0, "ok\n", "")


@pytest.fixture
def paths(tmp_path: Path) -> object:
    environment = {
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    return deploy_module.InstallPaths.discover(environment)


def source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_rendered_config_and_unit_use_persistent_paths_and_safe_service_type(paths: object) -> None:
    config = deploy_module.render_config(paths)
    unit = deploy_module.render_unit(paths)
    assert f"sqlite:///{paths.state}/machinedeck.db" in config
    assert "password" not in config.lower()
    assert "Type=simple" in unit
    assert "Type=notify" not in unit
    assert str(paths.current / "venv" / "bin" / "python") in unit
    assert "NoNewPrivileges=true" in unit
    assert "KillMode=control-group" in unit


def test_install_is_atomic_idempotent_and_preserves_configuration(
    paths: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(paths.state / "machinedeck.db")
    monkeypatch.setattr(deploy_module, "_port_available", lambda: True)
    monkeypatch.setattr(deploy_module, "_wait_for_health", lambda: None)

    release = deploy_module.deploy(source_root(), paths, runner, upgrade=False)
    original_config = paths.config.read_text()
    second = deploy_module.deploy(source_root(), paths, runner, upgrade=False)

    assert second == release
    assert paths.current.resolve() == release
    assert paths.config.read_text() == original_config
    assert paths.config.stat().st_mode & 0o777 == 0o600
    assert paths.unit.stat().st_mode & 0o777 == 0o644
    assert any(command[:3] == ["systemctl", "--user", "enable"] for command in runner.commands)
    assert any(command[:3] == ["systemd-analyze", "--user", "verify"] for command in runner.commands)


def test_upgrade_failure_restores_release_unit_and_database(
    paths: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_module, "_port_available", lambda: True)
    monkeypatch.setattr(deploy_module, "_wait_for_health", lambda: None)
    initial_runner = FakeRunner(paths.state / "machinedeck.db")
    old_release = deploy_module.deploy(source_root(), paths, initial_runner, upgrade=False)
    old_unit = paths.unit.read_bytes()
    with sqlite3.connect(paths.state / "machinedeck.db") as connection:
        connection.execute("DELETE FROM deployment_test")
        connection.execute("INSERT INTO deployment_test VALUES ('before-upgrade')")

    failing_runner = FakeRunner(
        paths.state / "machinedeck.db", active=True, fail_on="enable --now"
    )
    with pytest.raises(subprocess.CalledProcessError):
        deploy_module.deploy(source_root(), paths, failing_runner, upgrade=True)

    assert paths.current.resolve() == old_release
    assert paths.unit.read_bytes() == old_unit
    with sqlite3.connect(paths.state / "machinedeck.db") as connection:
        values = connection.execute("SELECT value FROM deployment_test").fetchall()
    assert values == [("before-upgrade",)]
    assert [path for path in paths.releases.iterdir() if not path.name.startswith(".")] == [old_release]


def test_first_install_daemon_reload_failure_removes_partial_state(
    paths: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_module, "_port_available", lambda: True)
    runner = FakeRunner(paths.state / "machinedeck.db", fail_on="daemon-reload")
    with pytest.raises(subprocess.CalledProcessError):
        deploy_module.deploy(source_root(), paths, runner, upgrade=False)
    assert not paths.current.exists()
    assert not paths.unit.exists()
    assert not paths.config.exists()
    assert not (paths.state / "machinedeck.db").exists()
    assert not [path for path in paths.releases.iterdir() if not path.name.startswith(".")]


def test_uninstall_preserves_data_and_managed_units_unless_explicitly_removed(
    paths: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_module, "_port_available", lambda: True)
    monkeypatch.setattr(deploy_module, "_wait_for_health", lambda: None)
    runner = FakeRunner(paths.state / "machinedeck.db")
    deploy_module.deploy(source_root(), paths, runner, upgrade=False)
    managed_unit = paths.user_unit_dir / "machinedeck-example.service"
    managed_unit.write_text(
        "# Generated by MachineDeck. Manual changes will be detected and replaced.\nfixture"
    )
    unrelated_unit = paths.user_unit_dir / "machinedeck-personal.service"
    unrelated_unit.write_text("user-owned unit that MachineDeck did not generate\n")

    deploy_module.uninstall(
        paths, runner, purge=False, remove_managed_units=False, yes=False
    )
    assert not paths.share.exists()
    assert paths.state.exists() and paths.config.exists() and managed_unit.exists()

    with pytest.raises(deploy_module.DeployError, match="require --yes"):
        deploy_module.uninstall(
            paths, runner, purge=True, remove_managed_units=True, yes=False
        )
    deploy_module.uninstall(
        paths, runner, purge=True, remove_managed_units=True, yes=True
    )
    assert not paths.state.exists() and not paths.config_dir.exists()
    assert not managed_unit.exists()
    assert unrelated_unit.exists()


def test_install_rejects_machine_deck_target_symlink(paths: object, tmp_path: Path) -> None:
    paths.share.parent.mkdir(parents=True)
    target = tmp_path / "redirected"
    target.mkdir()
    os.symlink(target, paths.share)
    runner = FakeRunner(paths.state / "machinedeck.db")
    with pytest.raises(deploy_module.DeployError, match="unsafe symlink"):
        deploy_module.deploy(source_root(), paths, runner, upgrade=False)
