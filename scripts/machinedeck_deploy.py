#!/usr/bin/env python3
"""Safe user-level installation lifecycle for MachineDeck."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4


SERVICE_NAME = "machinedeck.service"
MINIMUM_PYTHON = (3, 10)
DOCTOR_HEALTHY = 0
DOCTOR_WARNINGS = 1
DOCTOR_ERRORS = 2
DOCTOR_UNSUPPORTED = 3


class DeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallPaths:
    home: Path
    share: Path
    releases: Path
    current: Path
    state: Path
    backups: Path
    config_dir: Path
    config: Path
    user_unit_dir: Path
    unit: Path

    @classmethod
    def discover(cls, environment: dict[str, str] | None = None) -> "InstallPaths":
        env = environment or os.environ
        home = Path(env.get("HOME", str(Path.home()))).expanduser().absolute()
        data_home = Path(env.get("XDG_DATA_HOME", home / ".local" / "share")).expanduser().absolute()
        state_home = Path(env.get("XDG_STATE_HOME", home / ".local" / "state")).expanduser().absolute()
        config_home = Path(env.get("XDG_CONFIG_HOME", home / ".config")).expanduser().absolute()
        share = data_home / "machinedeck"
        state = state_home / "machinedeck"
        config_dir = config_home / "machinedeck"
        user_unit_dir = config_home / "systemd" / "user"
        return cls(
            home=home,
            share=share,
            releases=share / "releases",
            current=share / "current",
            state=state,
            backups=state / "backups",
            config_dir=config_dir,
            config=config_dir / "config.toml",
            user_unit_dir=user_unit_dir,
            unit=user_unit_dir / SERVICE_NAME,
        )


class CommandRunner:
    def run(
        self,
        arguments: Sequence[str | Path],
        *,
        check: bool = True,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(argument) for argument in arguments]
        try:
            return subprocess.run(
                command,
                check=check,
                cwd=cwd,
                env=env,
                text=True,
                capture_output=capture,
            )
        except FileNotFoundError as exc:
            if check:
                raise DeployError(f"Required command is unavailable: {command[0]}") from exc
            return subprocess.CompletedProcess(command, 127, "", str(exc))


def _reject_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise DeployError("Do not install MachineDeck as root or through sudo")


def _check_linux() -> None:
    if platform.system() != "Linux":
        raise DeployError("MachineDeck user-service installation requires Linux")
    if sys.version_info < MINIMUM_PYTHON:
        raise DeployError("MachineDeck requires Python 3.10 or newer")


def _validate_text(value: str, label: str) -> str:
    if not value or any(character in value for character in "\r\n\x00"):
        raise DeployError(f"Invalid {label}")
    return value


def _ensure_not_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise DeployError(f"Refusing unsafe symlink for {label}: {path}")


def _prepare_paths(paths: InstallPaths) -> None:
    for path, mode in (
        (paths.share, 0o755),
        (paths.releases, 0o755),
        (paths.state, 0o700),
        (paths.backups, 0o700),
        (paths.config_dir, 0o700),
        (paths.user_unit_dir, 0o755),
    ):
        _ensure_not_symlink(path, "installation path")
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        os.chmod(path, mode)
    _ensure_not_symlink(paths.config, "configuration")
    _ensure_not_symlink(paths.unit, "service unit")


def _validate_sqlite_writable(paths: InstallPaths) -> None:
    probe = paths.state / f".sqlite-write-test-{uuid4().hex}.db"
    try:
        with sqlite3.connect(probe) as connection:
            connection.execute("PRAGMA user_version")
        probe.unlink()
    except (OSError, sqlite3.Error) as exc:
        probe.unlink(missing_ok=True)
        raise DeployError(
            f"SQLite state directory is not writable: {paths.state}: {exc}"
        ) from exc


def _source_version(source: Path) -> str:
    pyproject = source / "backend" / "pyproject.toml"
    try:
        contents = pyproject.read_text(encoding="utf-8")
        project = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", contents)
        version_match = re.search(
            r'(?m)^version\s*=\s*["\']([^"\']+)["\']\s*$',
            project.group(1) if project else "",
        )
        if version_match is None:
            raise ValueError("missing project.version")
        version = version_match.group(1)
    except (OSError, ValueError) as exc:
        raise DeployError(f"Cannot determine source version from {pyproject}") from exc
    return _validate_text(str(version), "version")


def _validate_source(source: Path) -> Path:
    source = source.expanduser().resolve()
    required = (
        source / "backend" / "app",
        source / "backend" / "alembic",
        source / "backend" / "alembic.ini",
        source / "backend" / "pyproject.toml",
        source / "frontend" / "index.html",
        source / "scripts" / "machinedeck_deploy.py",
    )
    if not all(path.exists() for path in required):
        raise DeployError(f"Not a complete MachineDeck source tree: {source}")
    for root in (source / "backend" / "app", source / "backend" / "alembic", source / "frontend"):
        for candidate in root.rglob("*"):
            if candidate.is_symlink():
                raise DeployError(f"Source tree contains an unsafe symlink: {candidate}")
    return source


def _copy_source(source: Path, destination: Path) -> None:
    backend = destination / "backend"
    backend.mkdir(mode=0o755)
    shutil.copytree(source / "backend" / "app", backend / "app")
    shutil.copytree(source / "backend" / "alembic", backend / "alembic")
    shutil.copy2(source / "backend" / "alembic.ini", backend / "alembic.ini")
    shutil.copy2(source / "backend" / "pyproject.toml", backend / "pyproject.toml")
    shutil.copytree(source / "frontend", destination / "frontend")
    installed_scripts = destination / "scripts"
    installed_scripts.mkdir(mode=0o755)
    shutil.copy2(
        source / "scripts" / "machinedeck_deploy.py",
        installed_scripts / "machinedeck_deploy.py",
    )
    os.chmod(installed_scripts / "machinedeck_deploy.py", 0o755)
    for optional in ("README.md", "LICENSE"):
        if (source / optional).is_file():
            shutil.copy2(source / optional, destination / optional)


def _atomic_write(path: Path, content: str, mode: int) -> None:
    _ensure_not_symlink(path, "managed file")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def render_config(paths: InstallPaths) -> str:
    database = f"sqlite:///{paths.state / 'machinedeck.db'}"
    origins = [
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "https://127.0.0.1:8080",
        "https://localhost:8080",
    ]
    rendered_origins = ", ".join(_toml_string(origin) for origin in origins)
    rendered_roots = ", ".join(_toml_string(path) for path in (paths.home,))
    rendered_disks = ", ".join(_toml_string(path) for path in (Path("/"), paths.home))
    return (
        "# MachineDeck configuration. Administrator credentials are never stored here.\n"
        "[server]\n"
        'host = "127.0.0.1"\n'
        "port = 8080\n"
        f"trusted_origins = [{rendered_origins}]\n\n"
        "[state]\n"
        f"database_url = {_toml_string(database)}\n\n"
        "[paths]\n"
        f"allowed_roots = [{rendered_roots}]\n"
        f"monitor_disks = [{rendered_disks}]\n"
        f"user_unit_dir = {_toml_string(paths.user_unit_dir)}\n"
    )


def _systemd_quote(value: str | Path) -> str:
    text = _validate_text(str(value), "systemd value")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _systemd_path(value: str | Path) -> str:
    text = _validate_text(str(value), "systemd path")
    safe = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-"
    output: list[str] = []
    for byte in text.encode("utf-8"):
        if byte in safe:
            output.append(chr(byte))
        elif byte == ord("%"):
            output.append("%%")
        else:
            output.append(f"\\x{byte:02x}")
    return "".join(output)


def _systemd_environment(name: str, value: str | Path) -> str:
    _validate_text(name, "environment name")
    return f"Environment={_systemd_quote(f'{name}={value}')}"


def render_unit(paths: InstallPaths, release_root: Path | None = None) -> str:
    root = release_root or paths.current
    python = root / "venv" / "bin" / "python"
    working_directory = root / "backend"
    return "\n".join(
        [
            "[Unit]",
            "Description=MachineDeck Local Workload Control Plane",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={_systemd_path(working_directory)}",
            f"ExecStart={_systemd_quote(python)} -m uvicorn app.main:app --host 127.0.0.1 --port 8080",
            "Restart=on-failure",
            "RestartSec=5",
            "TimeoutStartSec=60",
            "TimeoutStopSec=30",
            "KillMode=control-group",
            _systemd_environment("MACHINEDECK_CONFIG", paths.config),
            _systemd_environment("MACHINEDECK_PROJECT_ROOT", root),
            "Environment=PYTHONUNBUFFERED=1",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "RestrictSUIDSGID=true",
            "LockPersonality=true",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def _database_path(paths: InstallPaths) -> Path:
    return paths.state / "machinedeck.db"


def _backup_database(paths: InstallPaths) -> Path | None:
    database = _database_path(paths)
    if not database.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = paths.backups / f"machinedeck-{timestamp}-{uuid4().hex[:8]}.db"
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    os.chmod(backup, 0o600)
    return backup


def _restore_database(paths: InstallPaths, backup: Path | None) -> None:
    if backup is None:
        return
    database = _database_path(paths)
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)
    temporary = database.with_name(f".{database.name}.restore-{uuid4().hex}")
    shutil.copy2(backup, temporary)
    os.replace(temporary, database)


def _remove_database(paths: InstallPaths) -> None:
    database = _database_path(paths)
    for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        candidate.unlink(missing_ok=True)


def _confirmed(prompt: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise DeployError(f"Confirmation required; rerun with --yes: {prompt}")
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


def _current_target(paths: InstallPaths) -> Path | None:
    if not paths.current.is_symlink():
        if paths.current.exists():
            raise DeployError(f"Current release pointer is not a symlink: {paths.current}")
        return None
    raw = Path(os.readlink(paths.current))
    target = raw if raw.is_absolute() else paths.current.parent / raw
    resolved = target.resolve()
    try:
        resolved.relative_to(paths.releases.resolve())
    except ValueError as exc:
        raise DeployError(f"Current release points outside the release directory: {resolved}") from exc
    return resolved


def _switch_current(paths: InstallPaths, release: Path) -> None:
    temporary = paths.share / f".current-{uuid4().hex}"
    os.symlink(release, temporary)
    os.replace(temporary, paths.current)


def _service_active(runner: CommandRunner) -> bool:
    result = runner.run(
        ["systemctl", "--user", "is-active", "--quiet", SERVICE_NAME],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def _best_effort(runner: CommandRunner, arguments: Sequence[str | Path]) -> None:
    try:
        runner.run(arguments, check=False, capture=True)
    except Exception:
        pass


def _is_generated_application_unit(path: Path) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        metadata = os.fstat(descriptor)
        if not path.is_file() or metadata.st_uid != os.geteuid() or metadata.st_size > 64 * 1024:
            return False
        content = os.read(descriptor, metadata.st_size).decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)
    return content.startswith(
        "# Generated by MachineDeck. Manual changes will be detected and replaced.\n"
    )


def _wait_for_health(port: int = 8080, attempts: int = 30) -> None:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    raise DeployError("MachineDeck did not pass its HTTP health check")


def _port_available(port: int = 8080) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _release_name(version: str, *, upgrade: bool) -> str:
    if not upgrade:
        return version
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{version}-{timestamp}-{uuid4().hex[:8]}"


def deploy(
    source: Path,
    paths: InstallPaths,
    runner: CommandRunner,
    *,
    upgrade: bool,
    start: bool = True,
    enable_linger: bool = False,
    assume_yes: bool = False,
) -> Path:
    _check_linux()
    _reject_root()
    source = _validate_source(source)
    _prepare_paths(paths)
    _validate_sqlite_writable(paths)
    old_release = _current_target(paths)
    if upgrade and old_release is None:
        raise DeployError("MachineDeck is not installed; run install first")
    if not upgrade and old_release is not None:
        print(f"MachineDeck is already installed at {old_release}")
        return old_release
    if not _port_available() and not _service_active(runner):
        raise DeployError("TCP port 8080 is already in use by another process")
    runner.run(["systemctl", "--user", "show-environment"], capture=True)

    version = _source_version(source)
    print(f"Install root: {paths.share}")
    print(f"State root: {paths.state}")
    print(f"Configuration: {paths.config}")
    release = paths.releases / _release_name(version, upgrade=upgrade)
    if release.exists() or release.is_symlink():
        incomplete = release / ".installing"
        if incomplete.is_file() and release != old_release:
            shutil.rmtree(release)
        else:
            raise DeployError(f"Release already exists: {release}")
    staging = release
    old_unit = paths.unit.read_bytes() if paths.unit.exists() else None
    backup: Path | None = None
    database_existed = _database_path(paths).exists()
    config_existed = paths.config.exists()
    stopped_old_service = False
    switched = False
    try:
        staging.mkdir(mode=0o755)
        _atomic_write(staging / ".installing", "incomplete\n", 0o600)
        _copy_source(source, staging)
        runner.run([sys.executable, "-m", "venv", staging / "venv"])
        runner.run(
            [staging / "venv" / "bin" / "python", "-m", "pip", "install", staging / "backend"]
        )
        _atomic_write(staging / "VERSION", f"{version}\n", 0o644)
        verification_unit = staging / SERVICE_NAME
        _atomic_write(verification_unit, render_unit(paths, staging), 0o644)
        runner.run(["systemd-analyze", "--user", "verify", verification_unit])
        verification_unit.unlink()
        if not paths.config.exists():
            _atomic_write(paths.config, render_config(paths), 0o600)

        if old_release is not None and _service_active(runner):
            runner.run(["systemctl", "--user", "stop", SERVICE_NAME])
            stopped_old_service = True
        backup = _backup_database(paths)
        migration_environment = os.environ.copy()
        migration_environment.update(
            {
                "MACHINEDECK_CONFIG": str(paths.config),
                "MACHINEDECK_DATABASE_URL": f"sqlite:///{_database_path(paths)}",
                "MACHINEDECK_PROJECT_ROOT": str(staging),
            }
        )
        runner.run(
            [staging / "venv" / "bin" / "alembic", "upgrade", "head"],
            cwd=staging / "backend",
            env=migration_environment,
        )
        os.chmod(_database_path(paths), 0o600)
        (staging / ".installing").unlink()
        _switch_current(paths, release)
        switched = True
        _atomic_write(paths.unit, render_unit(paths), 0o644)
        runner.run(["systemctl", "--user", "daemon-reload"])
        if enable_linger:
            if _confirmed(
                "MachineDeck will continue running after logout by enabling systemd linger",
                assume_yes=assume_yes,
            ):
                runner.run(["loginctl", "enable-linger", os.environ.get("USER", "")])
        if start:
            runner.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME])
            _wait_for_health()
        return release
    except Exception:
        if old_release is None:
            _best_effort(
                runner,
                ["systemctl", "--user", "disable", "--now", SERVICE_NAME],
            )
        else:
            _best_effort(runner, ["systemctl", "--user", "stop", SERVICE_NAME])
        if switched:
            if old_release is not None:
                _switch_current(paths, old_release)
            else:
                paths.current.unlink(missing_ok=True)
        if old_unit is None:
            paths.unit.unlink(missing_ok=True)
        else:
            descriptor, name = tempfile.mkstemp(prefix=".machinedeck-unit-", dir=paths.unit.parent)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(old_unit)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o644)
            os.replace(name, paths.unit)
        if backup is not None:
            _restore_database(paths, backup)
        elif not database_existed:
            _remove_database(paths)
        if not config_existed:
            paths.config.unlink(missing_ok=True)
        if release.exists() and release != old_release:
            shutil.rmtree(release)
        if staging.exists():
            shutil.rmtree(staging)
        _best_effort(runner, ["systemctl", "--user", "daemon-reload"])
        if stopped_old_service:
            _best_effort(runner, ["systemctl", "--user", "start", SERVICE_NAME])
        raise


def uninstall(
    paths: InstallPaths,
    runner: CommandRunner,
    *,
    purge: bool,
    remove_managed_units: bool,
    yes: bool,
) -> None:
    _reject_root()
    if (purge or remove_managed_units) and not yes:
        raise DeployError("Destructive uninstall options require --yes")
    for target in (paths.share, paths.state, paths.config_dir, paths.user_unit_dir):
        _ensure_not_symlink(target, "uninstall target")
    runner.run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], check=False)
    paths.unit.unlink(missing_ok=True)
    runner.run(["systemctl", "--user", "daemon-reload"], check=False)
    if paths.share.exists():
        shutil.rmtree(paths.share)
    if remove_managed_units and paths.user_unit_dir.exists():
        for unit in paths.user_unit_dir.glob("machinedeck-*.service"):
            if unit.name not in {SERVICE_NAME, "machinedeck-phase0.service"} and _is_generated_application_unit(unit):
                _ensure_not_symlink(unit, "managed application unit")
                unit.unlink()
        runner.run(["systemctl", "--user", "daemon-reload"], check=False)
    if purge:
        if paths.state.exists():
            shutil.rmtree(paths.state)
        if paths.config_dir.exists():
            shutil.rmtree(paths.config_dir)


@dataclass(frozen=True)
class DoctorCheck:
    status: str
    name: str
    detail: str


def doctor(paths: InstallPaths, runner: CommandRunner) -> tuple[int, list[DoctorCheck]]:
    checks: list[DoctorCheck] = []

    def record(status: str, name: str, detail: str) -> None:
        checks.append(DoctorCheck(status, name, detail))

    if platform.system() != "Linux":
        record("ERROR", "Platform", "Linux is required")
        return DOCTOR_UNSUPPORTED, checks
    try:
        release = _current_target(paths)
    except DeployError as exc:
        record("ERROR", "Release pointer", str(exc))
        release = None
    if release and (release / "VERSION").is_file():
        record("PASS", "Version", (release / "VERSION").read_text().strip())
    else:
        record("ERROR", "Installation", "No installed release")
    record("PASS", "Python", platform.python_version())
    for name, path in (("Configuration", paths.config), ("Database", _database_path(paths)), ("User unit", paths.unit)):
        if path.is_symlink():
            record("ERROR", name, f"Unsafe symlink: {path}")
        elif path.exists():
            record("PASS", name, str(path))
        else:
            record("ERROR", name, f"Missing: {path}")
    if paths.config.exists() and not paths.config.is_symlink():
        mode = paths.config.stat().st_mode & 0o777
        record("PASS" if mode & 0o077 == 0 else "ERROR", "Configuration permissions", oct(mode))
    database = _database_path(paths)
    if database.exists() and not database.is_symlink():
        database_mode = database.stat().st_mode & 0o777
        record(
            "PASS" if database_mode & 0o077 == 0 else "ERROR",
            "Database permissions",
            oct(database_mode),
        )
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            record("PASS", "Database migration", revision[0] if revision else "no revision")
        except sqlite3.Error as exc:
            record("ERROR", "Database connection", str(exc))
    user_bus = runner.run(
        ["systemctl", "--user", "show-environment"], check=False, capture=True
    )
    record(
        "PASS" if user_bus.returncode == 0 else "ERROR",
        "systemd user bus",
        "available" if user_bus.returncode == 0 else (user_bus.stderr or "unavailable").strip(),
    )
    service = runner.run(
        ["systemctl", "--user", "is-active", SERVICE_NAME], check=False, capture=True
    )
    record("PASS" if service.returncode == 0 else "ERROR", "systemd user service", (service.stdout or "inactive").strip())
    service_pid = runner.run(
        ["systemctl", "--user", "show", SERVICE_NAME, "-p", "MainPID", "--value"],
        check=False,
        capture=True,
    )
    pid = service_pid.stdout.strip()
    record(
        "PASS" if service_pid.returncode == 0 and pid not in {"", "0"} else "ERROR",
        "Service PID",
        pid or "unavailable",
    )
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=2) as response:
            healthy = response.status == 200
        record("PASS" if healthy else "ERROR", "HTTP health endpoint", f"HTTP {response.status}")
    except (OSError, urllib.error.URLError) as exc:
        record("ERROR", "HTTP health endpoint", str(exc))
    docker = runner.run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False, capture=True)
    record("PASS" if docker.returncode == 0 else "WARN", "Docker Engine", (docker.stdout or docker.stderr or "unavailable").strip())
    compose = runner.run(["docker", "compose", "version"], check=False, capture=True)
    record("PASS" if compose.returncode == 0 else "WARN", "Docker Compose", (compose.stdout or compose.stderr or "unavailable").strip())
    journal = runner.run(
        ["journalctl", "--user", "--unit", SERVICE_NAME, "--lines", "1", "--no-pager"],
        check=False,
        capture=True,
    )
    record("PASS" if journal.returncode == 0 else "WARN", "Journal access", "available" if journal.returncode == 0 else (journal.stderr or "unavailable").strip())
    if release:
        gpu_probe = (
            "import pynvml; pynvml.nvmlInit(); "
            "print(pynvml.nvmlDeviceGetCount()); pynvml.nvmlShutdown()"
        )
        nvml = runner.run(
            [release / "venv" / "bin" / "python", "-c", gpu_probe],
            check=False,
            capture=True,
        )
        record(
            "PASS" if nvml.returncode == 0 else "WARN",
            "NVIDIA NVML",
            f"{nvml.stdout.strip()} GPU(s)" if nvml.returncode == 0 else (nvml.stderr or "unavailable").strip(),
        )
    managed_units = [
        unit
        for unit in paths.user_unit_dir.glob("machinedeck-*.service")
        if unit.name != SERVICE_NAME
    ] if paths.user_unit_dir.exists() else []
    unsafe_units = [unit for unit in managed_units if unit.is_symlink()]
    record(
        "ERROR" if unsafe_units else "PASS",
        "Managed units",
        f"{len(managed_units)} present" if not unsafe_units else f"{len(unsafe_units)} unsafe symlink(s)",
    )
    linger = runner.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"], check=False, capture=True)
    linger_enabled = linger.returncode == 0 and linger.stdout.strip() == "yes"
    record("PASS" if linger_enabled else "WARN", "systemd linger", "enabled" if linger_enabled else "disabled")
    try:
        usage = shutil.disk_usage(paths.state if paths.state.exists() else paths.home)
        free_percent = usage.free / usage.total * 100 if usage.total else 0
        record("WARN" if free_percent < 10 else "PASS", "Disk space", f"{free_percent:.1f}% free")
    except OSError as exc:
        record("WARN", "Disk space", str(exc))
    if release is None:
        return DOCTOR_UNSUPPORTED, checks
    if any(check.status == "ERROR" for check in checks):
        return DOCTOR_ERRORS, checks
    if any(check.status == "WARN" for check in checks):
        return DOCTOR_WARNINGS, checks
    return DOCTOR_HEALTHY, checks


def _source_default() -> Path:
    return Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="machinedeck-deploy")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--from-local", type=Path, default=_source_default())
    install_parser.add_argument("--enable-linger", action="store_true")
    install_parser.add_argument("--no-start", action="store_true")
    install_parser.add_argument("--yes", action="store_true")
    upgrade_parser = subparsers.add_parser("upgrade")
    upgrade_parser.add_argument("--from-local", type=Path, default=_source_default())
    upgrade_parser.add_argument("--no-start", action="store_true")
    upgrade_parser.add_argument("--yes", action="store_true")
    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("--purge", action="store_true")
    uninstall_parser.add_argument("--remove-managed-units", action="store_true")
    uninstall_parser.add_argument("--yes", action="store_true")
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    paths = InstallPaths.discover()
    runner = CommandRunner()
    try:
        if arguments.command == "install":
            release = deploy(
                arguments.from_local,
                paths,
                runner,
                upgrade=False,
                start=not arguments.no_start,
                enable_linger=arguments.enable_linger,
                assume_yes=arguments.yes,
            )
            print(f"MachineDeck installed: {release}")
            print("Local URL: http://127.0.0.1:8080")
            return 0
        if arguments.command == "upgrade":
            release = deploy(
                arguments.from_local,
                paths,
                runner,
                upgrade=True,
                start=not arguments.no_start,
            )
            print(f"MachineDeck upgraded: {release}")
            return 0
        if arguments.command == "uninstall":
            uninstall(
                paths,
                runner,
                purge=arguments.purge,
                remove_managed_units=arguments.remove_managed_units,
                yes=arguments.yes,
            )
            print("MachineDeck application and service were removed.")
            if not arguments.purge:
                print(f"Preserved state: {paths.state}")
                print(f"Preserved configuration: {paths.config_dir}")
            if not arguments.remove_managed_units:
                print(f"Preserved managed units: {paths.user_unit_dir}/machinedeck-*.service")
            return 0
        code, checks = doctor(paths, runner)
        if arguments.json:
            print(json.dumps({"result": code, "checks": [check.__dict__ for check in checks]}, indent=2))
        else:
            print("MachineDeck Doctor\n")
            for check in checks:
                print(f"[{check.status}] {check.name}: {check.detail}")
            labels = {0: "HEALTHY", 1: "HEALTHY WITH WARNINGS", 2: "ERRORS", 3: "UNSUPPORTED"}
            print(f"\nResult: {labels[code]}")
        return code
    except DeployError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed ({exc.returncode}): {' '.join(exc.cmd)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
