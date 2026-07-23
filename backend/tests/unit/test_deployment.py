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
        self.environments: list[dict[str, str] | None] = []

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
        self.environments.append(env)
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
        if command[1:3] == ["-m", "pip"]:
            console_script = Path(command[0]).parent / "machinedeck"
            console_script.write_text(f"#!{command[0]}\n")
            console_script.chmod(0o755)
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
    assert "ProtectKernelModules" not in unit
    assert "ProtectKernelTunables" not in unit
    assert "ProtectControlGroups" not in unit
    assert f"WorkingDirectory={paths.current}/backend" in unit
    assert f'Environment="MACHINEDECK_CONFIG={paths.config}"' in unit
    assert "--no-proxy-headers" in unit


def test_access_profiles_render_mode_aware_binding_origins_and_cookie_policy(
    paths: object,
) -> None:
    local = deploy_module.access_configuration("local")
    lan = deploy_module.access_configuration(
        "lan",
        hostname="machine",
        addresses=("192.168.1.50",),
    )
    proxy = deploy_module.access_configuration(
        "tailscale",
        trusted_origins=("https://machine.example.ts.net",),
        trusted_networks=("100.64.0.0/10",),
    )

    assert (local.mode, local.host, local.cookie_secure) == (
        "local",
        "127.0.0.1",
        "auto",
    )
    assert (lan.mode, lan.host, lan.public_host_lan) == (
        "lan",
        "0.0.0.0",
        "machine",
    )
    assert "http://machine:8080" in lan.trusted_origins
    assert "http://192.168.1.50:8080" in lan.trusted_origins
    assert (proxy.mode, proxy.host) == ("proxy", "127.0.0.1")
    assert "https://machine.example.ts.net" in proxy.trusted_origins
    assert proxy.trusted_proxies == ("127.0.0.1/32", "::1/128")
    assert "100.64.0.0/10" in proxy.trusted_networks

    config = deploy_module.render_config(paths, lan)
    unit = deploy_module.render_unit(paths, server=lan)
    assert 'mode = "lan"' in config
    assert 'host = "0.0.0.0"' in config
    assert 'cookie_secure = "auto"' in config
    assert 'public_host_lan = "machine"' in config
    assert '--host "0.0.0.0" --port 8080' in unit
    assert "trusted_networks" in config


def test_access_profiles_reject_inconsistent_or_unsafe_configuration(paths: object) -> None:
    with pytest.raises(deploy_module.DeployError, match="HTTPS"):
        deploy_module.access_configuration(
            "tailscale",
            trusted_origins=("http://machine.example.ts.net",),
        )
    with pytest.raises(deploy_module.DeployError, match="loopback"):
        deploy_module.validate_server_configuration(
            deploy_module.ServerConfiguration(
                mode="proxy",
                host="0.0.0.0",
                port=8080,
                cookie_secure="auto",
            )
        )


def test_read_server_configuration_supports_legacy_and_new_files(paths: object) -> None:
    paths.config.parent.mkdir(parents=True)
    paths.config.write_text(
        '[server]\nhost = "127.0.0.1"\nport = 8080\n'
    )
    legacy = deploy_module.read_server_configuration(paths.config)
    assert legacy.mode == "local"
    assert legacy.cookie_secure is True

    lan = deploy_module.access_configuration(
        "lan", hostname="machine", addresses=("192.168.1.50",)
    )
    paths.config.write_text(deploy_module.render_config(paths, lan))
    loaded = deploy_module.read_server_configuration(paths.config)
    assert loaded == lan


def test_unit_paths_are_escaped_without_turning_quotes_into_path_characters(tmp_path: Path) -> None:
    paths = deploy_module.InstallPaths.discover(
        {
            "HOME": str(tmp_path / "home with spaces"),
            "XDG_DATA_HOME": str(tmp_path / "data with spaces"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config with spaces"),
        }
    )
    unit = deploy_module.render_unit(paths)
    assert "WorkingDirectory=\"" not in unit
    assert "\\x20" in next(
        line for line in unit.splitlines() if line.startswith("WorkingDirectory=")
    )
    assert f'Environment="MACHINEDECK_CONFIG={paths.config}"' in unit


def test_install_is_atomic_idempotent_and_preserves_configuration(
    paths: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(paths.state / "machinedeck.db")
    monkeypatch.setattr(deploy_module, "_port_available", lambda *_: True)
    monkeypatch.setattr(deploy_module, "_wait_for_health", lambda *_: None)

    release = deploy_module.deploy(source_root(), paths, runner, upgrade=False)
    original_config = paths.config.read_text()
    second = deploy_module.deploy(source_root(), paths, runner, upgrade=False)

    assert second == release
    assert paths.current.resolve() == release
    assert paths.config.read_text() == original_config
    assert paths.config.stat().st_mode & 0o777 == 0o600
    assert paths.unit.stat().st_mode & 0o777 == 0o644
    assert (paths.state / "machinedeck.db").stat().st_mode & 0o777 == 0o600
    assert (release / "venv" / "bin" / "machinedeck").read_text().splitlines()[0] == (
        f"#!{release}/venv/bin/python"
    )
    assert not (release / ".installing").exists()
    assert any(command[:3] == ["systemctl", "--user", "enable"] for command in runner.commands)
    assert any(command[:3] == ["systemd-analyze", "--user", "verify"] for command in runner.commands)
    migration_env = next(
        environment
        for command, environment in zip(runner.commands, runner.environments)
        if command and command[0].endswith("/alembic")
    )
    assert migration_env["MACHINEDECK_DATABASE_URL"] == (
        f"sqlite:///{paths.state}/machinedeck.db"
    )


def test_lan_install_and_upgrade_preserve_selected_access_mode(
    paths: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(paths.state / "machinedeck.db")
    monkeypatch.setattr(deploy_module, "_port_available", lambda *_: True)
    monkeypatch.setattr(deploy_module, "_wait_for_health", lambda *_: None)
    monkeypatch.setattr(
        deploy_module,
        "_detected_lan_hosts",
        lambda: ("machine", ("192.168.1.50",)),
    )

    deploy_module.deploy(
        source_root(),
        paths,
        runner,
        upgrade=False,
        access_mode="lan",
    )
    original_config = paths.config.read_text()
    assert '--host "0.0.0.0" --port 8080' in paths.unit.read_text()

    deploy_module.deploy(
        source_root(),
        paths,
        FakeRunner(paths.state / "machinedeck.db", active=True),
        upgrade=True,
    )
    assert paths.config.read_text() == original_config
    assert deploy_module.read_server_configuration(paths.config).mode == "lan"
    assert '--host "0.0.0.0" --port 8080' in paths.unit.read_text()


def test_upgrade_failure_restores_release_unit_and_database(
    paths: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_module, "_port_available", lambda *_: True)
    monkeypatch.setattr(deploy_module, "_wait_for_health", lambda *_: None)
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
    monkeypatch.setattr(deploy_module, "_port_available", lambda *_: True)
    runner = FakeRunner(paths.state / "machinedeck.db", fail_on="daemon-reload")
    with pytest.raises(subprocess.CalledProcessError):
        deploy_module.deploy(source_root(), paths, runner, upgrade=False)
    assert not paths.current.exists()
    assert not paths.unit.exists()
    assert not paths.config.exists()
    assert not (paths.state / "machinedeck.db").exists()
    assert not [path for path in paths.releases.iterdir() if not path.name.startswith(".")]


def test_failed_health_check_disables_partially_enabled_first_install(
    paths: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_module, "_port_available", lambda *_: True)

    def unhealthy(*_: object) -> None:
        raise deploy_module.DeployError("health check failed")

    monkeypatch.setattr(deploy_module, "_wait_for_health", unhealthy)
    runner = FakeRunner(paths.state / "machinedeck.db")
    with pytest.raises(deploy_module.DeployError, match="health check failed"):
        deploy_module.deploy(source_root(), paths, runner, upgrade=False)
    assert ["systemctl", "--user", "disable", "--now", "machinedeck.service"] in runner.commands
    assert not paths.current.exists()
    assert not paths.unit.exists()
    assert not (paths.state / "machinedeck.db").exists()


def test_uninstall_preserves_data_and_managed_units_unless_explicitly_removed(
    paths: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_module, "_port_available", lambda *_: True)
    monkeypatch.setattr(deploy_module, "_wait_for_health", lambda *_: None)
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


def test_sqlite_write_preflight_reports_state_path(
    paths: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy_module._prepare_paths(paths)

    def denied(_: object) -> None:
        raise sqlite3.OperationalError("permission denied")

    monkeypatch.setattr(deploy_module.sqlite3, "connect", denied)
    with pytest.raises(deploy_module.DeployError, match=str(paths.state)):
        deploy_module._validate_sqlite_writable(paths)
