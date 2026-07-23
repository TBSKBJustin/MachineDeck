#!/usr/bin/env python3
"""Safe user-level installation lifecycle for MachineDeck."""

from __future__ import annotations

import argparse
import ipaddress
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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


SERVICE_NAME = "machinedeck.service"
MINIMUM_PYTHON = (3, 10)
DOCTOR_HEALTHY = 0
DOCTOR_WARNINGS = 1
DOCTOR_ERRORS = 2
DOCTOR_UNSUPPORTED = 3
INSTALL_ACCESS_MODES = {"local", "lan", "tailscale"}
SERVER_ACCESS_MODES = {"local", "lan", "proxy"}
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


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


@dataclass(frozen=True)
class ServerConfiguration:
    mode: str
    host: str
    port: int
    cookie_secure: str | bool
    trusted_origins: tuple[str, ...] = ()
    public_host_lan: str | None = None
    trusted_proxies: tuple[str, ...] = ()
    trusted_networks: tuple[str, ...] = ("127.0.0.0/8", "::1/128")


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


def _validated_origin(value: str, *, https_required: bool = False) -> str:
    normalized = _validate_text(value.strip().rstrip("/"), "trusted Origin")
    parsed = urllib.parse.urlsplit(normalized)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise DeployError(f"Invalid trusted Origin: {value}") from exc
    allowed_schemes = {"https"} if https_required else {"http", "https"}
    if https_required and parsed.scheme != "https":
        raise DeployError(f"Trusted Origin must use HTTPS: {value}")
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DeployError(f"Invalid trusted Origin: {value}")
    return normalized


def _detected_lan_hosts() -> tuple[str, tuple[str, ...]]:
    hostname = socket.gethostname().strip().lower()
    if not hostname or any(character in hostname for character in "\r\n\x00/:"):
        hostname = "localhost"
    addresses: set[str] = set()
    try:
        candidates = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        candidates = []
    for candidate in candidates:
        raw = candidate[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if not address.is_loopback and not address.is_unspecified and not address.is_link_local:
            addresses.add(str(address))
    return hostname, tuple(sorted(addresses))


def access_configuration(
    access: str,
    *,
    trusted_origins: Sequence[str] = (),
    trusted_proxies: Sequence[str] = (),
    trusted_networks: Sequence[str] = (),
    hostname: str | None = None,
    addresses: Sequence[str] | None = None,
) -> ServerConfiguration:
    if access not in INSTALL_ACCESS_MODES:
        raise DeployError(f"Unsupported access mode: {access}")
    extra_origins = tuple(
        _validated_origin(origin, https_required=access == "tailscale")
        for origin in trusted_origins
    )
    configured_proxies = tuple(str(value).strip() for value in trusted_proxies)
    configured_networks = tuple(str(value).strip() for value in trusted_networks)
    default_networks = ("127.0.0.0/8", "::1/128")
    local_origins = (
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "https://127.0.0.1:8080",
        "https://localhost:8080",
    )
    if access == "local":
        return ServerConfiguration(
            mode="local",
            host="127.0.0.1",
            port=8080,
            cookie_secure="auto",
            trusted_origins=tuple(dict.fromkeys((*local_origins, *extra_origins))),
            trusted_proxies=configured_proxies,
            trusted_networks=tuple(
                dict.fromkeys((*default_networks, *configured_networks))
            ),
        )
    if access == "tailscale":
        return ServerConfiguration(
            mode="proxy",
            host="127.0.0.1",
            port=8080,
            cookie_secure="auto",
            trusted_origins=tuple(dict.fromkeys((*local_origins, *extra_origins))),
            trusted_proxies=tuple(
                dict.fromkeys(("127.0.0.1/32", "::1/128", *configured_proxies))
            ),
            trusted_networks=tuple(
                dict.fromkeys((*default_networks, *configured_networks))
            ),
        )
    detected_hostname, detected_addresses = _detected_lan_hosts()
    lan_hostname = (hostname or detected_hostname).strip().lower()
    lan_addresses = tuple(addresses) if addresses is not None else detected_addresses
    lan_origins = [f"http://{lan_hostname}:8080"]
    for raw_address in lan_addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise DeployError(f"Invalid detected LAN address: {raw_address}") from exc
        host = f"[{address}]" if address.version == 6 else str(address)
        lan_origins.append(f"http://{host}:8080")
    return ServerConfiguration(
        mode="lan",
        host="0.0.0.0",
        port=8080,
        cookie_secure="auto",
        trusted_origins=tuple(
            dict.fromkeys((*local_origins, *lan_origins, *extra_origins))
        ),
        public_host_lan=lan_hostname,
        trusted_proxies=configured_proxies,
        trusted_networks=tuple(
            dict.fromkeys((*default_networks, *configured_networks))
        ),
    )


def _minimal_server_configuration(path: Path) -> ServerConfiguration:
    section = ""
    values: dict[tuple[str, str], str | int | bool] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        key, separator, raw_value = line.partition("=")
        if not separator or section not in {"server", "security", "network"}:
            continue
        name = key.strip()
        if name not in {
            "mode",
            "host",
            "port",
            "cookie_secure",
            "trusted_proxies",
            "trusted_networks",
        }:
            continue
        value = raw_value.strip()
        try:
            parsed: str | int | bool = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DeployError(
                "Python 3.10 installations require tomli to read customized TOML values"
            ) from exc
        values[(section, name)] = parsed
    host = str(values.get(("server", "host"), "127.0.0.1"))
    inferred_mode = "lan" if host in {"0.0.0.0", "::"} else "local"
    try:
        configuration = ServerConfiguration(
            mode=str(values.get(("server", "mode"), inferred_mode)),
            host=host,
            port=int(values.get(("server", "port"), 8080)),
            cookie_secure=values.get(("security", "cookie_secure"), True),
            trusted_proxies=tuple(
                str(value)
                for value in values.get(("network", "trusted_proxies"), [])
            ),
            trusted_networks=tuple(
                str(value)
                for value in values.get(
                    ("network", "trusted_networks"),
                    ["127.0.0.0/8", "::1/128"],
                )
            ),
        )
    except (TypeError, ValueError) as exc:
        raise DeployError("server.port must be an integer") from exc
    validate_server_configuration(configuration)
    return configuration


def read_server_configuration(path: Path) -> ServerConfiguration:
    try:
        if tomllib is None:
            return _minimal_server_configuration(path)
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as exc:
        raise DeployError(f"Cannot read MachineDeck configuration: {path}: {exc}") from exc
    server = data.get("server", {})
    security = data.get("security", {})
    network = data.get("network", {})
    if (
        not isinstance(server, dict)
        or not isinstance(security, dict)
        or not isinstance(network, dict)
    ):
        raise DeployError(
            "MachineDeck server, security, and network configuration must be TOML tables"
        )
    host = str(server.get("host", "127.0.0.1")).strip()
    inferred_mode = "lan" if host in {"0.0.0.0", "::"} else "local"
    origins = server.get("trusted_origins", [])
    if not isinstance(origins, list):
        raise DeployError("server.trusted_origins must be a TOML array")
    proxies = network.get("trusted_proxies", [])
    networks = network.get(
        "trusted_networks", ["127.0.0.0/8", "::1/128"]
    )
    if not isinstance(proxies, list) or not isinstance(networks, list):
        raise DeployError(
            "network.trusted_proxies and network.trusted_networks must be TOML arrays"
        )
    try:
        configuration = ServerConfiguration(
            mode=str(server.get("mode", inferred_mode)).strip().lower(),
            host=host,
            port=int(server.get("port", 8080)),
            cookie_secure=security.get("cookie_secure", True),
            trusted_origins=tuple(str(origin).rstrip("/") for origin in origins),
            public_host_lan=server.get("public_host_lan"),
            trusted_proxies=tuple(str(value) for value in proxies),
            trusted_networks=tuple(str(value) for value in networks),
        )
    except (TypeError, ValueError) as exc:
        raise DeployError("server.port must be an integer") from exc
    validate_server_configuration(configuration)
    return configuration


def validate_server_configuration(configuration: ServerConfiguration) -> None:
    if configuration.mode not in SERVER_ACCESS_MODES:
        raise DeployError("server.mode must be local, lan, or proxy")
    try:
        address = ipaddress.ip_address(configuration.host)
    except ValueError as exc:
        raise DeployError("server.host must be a literal IP address") from exc
    if configuration.mode in {"local", "proxy"} and not address.is_loopback:
        raise DeployError(f"server.mode={configuration.mode} requires a loopback host")
    if configuration.mode == "lan" and not address.is_unspecified:
        raise DeployError("server.mode=lan requires host 0.0.0.0 or ::")
    if not 1 <= configuration.port <= 65535:
        raise DeployError("server.port must be between 1 and 65535")
    cookie = configuration.cookie_secure
    if not isinstance(cookie, bool) and str(cookie).lower() not in {
        "auto",
        "secure",
        "insecure",
        "true",
        "false",
    }:
        raise DeployError("security.cookie_secure has an invalid value")
    for origin in configuration.trusted_origins:
        _validated_origin(origin)
    for label, values in (
        ("trusted_proxies", configuration.trusted_proxies),
        ("trusted_networks", configuration.trusted_networks),
    ):
        for value in values:
            try:
                network = ipaddress.ip_network(value, strict=True)
            except ValueError as exc:
                raise DeployError(
                    f"network.{label} must contain canonical IPv4 or IPv6 CIDRs"
                ) from exc
            if label == "trusted_proxies" and network.num_addresses > 65536:
                raise DeployError(
                    "network.trusted_proxies entries cannot contain more than 65536 addresses"
                )


def render_config(paths: InstallPaths, configuration: ServerConfiguration | None = None) -> str:
    configuration = configuration or access_configuration("local")
    validate_server_configuration(configuration)
    database = f"sqlite:///{paths.state / 'machinedeck.db'}"
    rendered_origins = ", ".join(
        _toml_string(origin) for origin in configuration.trusted_origins
    )
    rendered_roots = ", ".join(_toml_string(path) for path in (paths.home,))
    rendered_disks = ", ".join(_toml_string(path) for path in (Path("/"), paths.home))
    rendered_proxies = ", ".join(
        _toml_string(value) for value in configuration.trusted_proxies
    )
    rendered_networks = ", ".join(
        _toml_string(value) for value in configuration.trusted_networks
    )
    cookie_secure = (
        "true"
        if configuration.cookie_secure is True
        else "false"
        if configuration.cookie_secure is False
        else _toml_string(str(configuration.cookie_secure))
    )
    public_host = (
        f"public_host_lan = {_toml_string(configuration.public_host_lan)}\n"
        if configuration.public_host_lan
        else ""
    )
    return (
        "# MachineDeck configuration. Administrator credentials are never stored here.\n"
        "[server]\n"
        f"mode = {_toml_string(configuration.mode)}\n"
        f"host = {_toml_string(configuration.host)}\n"
        f"port = {configuration.port}\n"
        f"{public_host}"
        f"trusted_origins = [{rendered_origins}]\n\n"
        "[security]\n"
        f"cookie_secure = {cookie_secure}\n\n"
        "[network]\n"
        f"trusted_proxies = [{rendered_proxies}]\n"
        f"trusted_networks = [{rendered_networks}]\n\n"
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


def render_unit(
    paths: InstallPaths,
    release_root: Path | None = None,
    server: ServerConfiguration | None = None,
) -> str:
    root = release_root or paths.current
    server = server or (
        read_server_configuration(paths.config)
        if paths.config.exists()
        else access_configuration("local")
    )
    validate_server_configuration(server)
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
            (
                f"ExecStart={_systemd_quote(python)} -m uvicorn app.main:app "
                f"--host {_systemd_quote(server.host)} --port {server.port} "
                "--no-proxy-headers"
            ),
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
    access_mode: str | None = None,
    trusted_origins: Sequence[str] = (),
    trusted_proxies: Sequence[str] = (),
    trusted_networks: Sequence[str] = (),
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
    server = (
        read_server_configuration(paths.config)
        if paths.config.exists()
        else access_configuration(
            access_mode or "local",
            trusted_origins=trusted_origins,
            trusted_proxies=trusted_proxies,
            trusted_networks=trusted_networks,
        )
    )
    if not _port_available(server.port) and not _service_active(runner):
        raise DeployError(f"TCP port {server.port} is already in use by another process")
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
        _atomic_write(
            verification_unit,
            render_unit(paths, staging, server),
            0o644,
        )
        runner.run(["systemd-analyze", "--user", "verify", verification_unit])
        verification_unit.unlink()
        if not paths.config.exists():
            _atomic_write(paths.config, render_config(paths, server), 0o600)

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
        active_server = read_server_configuration(paths.config)
        _atomic_write(paths.unit, render_unit(paths, server=active_server), 0o644)
        runner.run(["systemctl", "--user", "daemon-reload"])
        if enable_linger:
            if _confirmed(
                "MachineDeck will continue running after logout by enabling systemd linger",
                assume_yes=assume_yes,
            ):
                runner.run(["loginctl", "enable-linger", os.environ.get("USER", "")])
        if start:
            runner.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME])
            _wait_for_health(active_server.port)
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


@dataclass(frozen=True)
class FirewallAssessment:
    status: str
    detail: str


def _ufw_target_includes_port(target: str, port: int) -> bool:
    normalized = target.lower().replace("(v6)", "").strip()
    port_text = normalized.partition("/")[0]
    if ":" in port_text:
        start_text, _, end_text = port_text.partition(":")
        try:
            return int(start_text) <= port <= int(end_text)
        except ValueError:
            return False
    try:
        return int(port_text) == port
    except ValueError:
        return False


def _ufw_allow_sources(output: str, port: int) -> tuple[bool, list[IPNetwork]]:
    anywhere = False
    networks: list[IPNetwork] = []
    rule_pattern = re.compile(
        r"^\s*(?:\[\s*\d+\]\s*)?"
        r"(?P<target>\S+(?:\s+\(v6\))?)\s+"
        r"ALLOW(?:\s+IN)?\s+"
        r"(?P<source>.+?)\s*$",
        re.IGNORECASE,
    )
    for line in output.splitlines():
        match = rule_pattern.match(line)
        if match is None or not _ufw_target_includes_port(match.group("target"), port):
            continue
        source = match.group("source").strip()
        if source.lower().startswith("anywhere"):
            anywhere = True
            continue
        source_text = source.split()[0]
        try:
            network = ipaddress.ip_network(source_text, strict=False)
        except ValueError:
            continue
        if network not in networks:
            networks.append(network)
    return anywhere, networks


def _external_trusted_networks(
    configuration: ServerConfiguration,
) -> list[IPNetwork]:
    networks: list[IPNetwork] = []
    for value in configuration.trusted_networks:
        network = ipaddress.ip_network(value, strict=True)
        if network.is_loopback:
            continue
        networks.append(network)
    return networks


def _ufw_suggestion(configuration: ServerConfiguration) -> str | None:
    networks = _external_trusted_networks(configuration)
    if not networks:
        return None
    return (
        f"sudo ufw allow from {networks[0]} to any port "
        f"{configuration.port} proto tcp"
    )


def assess_ufw_status(
    output: str,
    configuration: ServerConfiguration,
) -> FirewallAssessment:
    normalized = output.strip()
    status_match = re.search(
        r"(?mi)^\s*Status:\s*(?P<status>active|inactive)\s*$", normalized
    )
    if status_match is None:
        return FirewallAssessment(
            "UNKNOWN",
            "UFW output could not be interpreted; firewall safety was not determined",
        )
    if status_match.group("status").lower() == "inactive":
        return FirewallAssessment(
            "WARN",
            "UFW is inactive while LAN mode listens on all IPv4 interfaces",
        )
    anywhere, allowed_networks = _ufw_allow_sources(
        normalized, configuration.port
    )
    suggestion = _ufw_suggestion(configuration)
    if anywhere:
        detail = (
            f"UFW permits TCP port {configuration.port} from Anywhere; "
            "the rule may expose every connected interface"
        )
        if suggestion:
            detail += f"\nSuggested subnet-scoped rule: {suggestion}"
        return FirewallAssessment("WARN", detail)
    expected = _external_trusted_networks(configuration)
    if not expected:
        return FirewallAssessment(
            "WARN",
            "No non-loopback trusted network is configured, so a scoped UFW rule cannot be evaluated",
        )
    missing = [
        network
        for network in expected
        if not any(
            network.version == allowed.version and network.subnet_of(allowed)
            for allowed in allowed_networks
        )
    ]
    if missing:
        detail = (
            f"No complete subnet-scoped UFW allowance was detected for TCP port "
            f"{configuration.port}: {', '.join(str(network) for network in missing)}"
        )
        if suggestion:
            detail += f"\nSuggested command: {suggestion}"
        return FirewallAssessment("WARN", detail)
    return FirewallAssessment(
        "PASS",
        (
            f"UFW is active with subnet-scoped TCP port {configuration.port} "
            f"allowance for {', '.join(str(network) for network in expected)}; "
            "this is a diagnostic, not proof of complete firewall safety"
        ),
    )


def firewall_doctor_check(
    configuration: ServerConfiguration,
    runner: CommandRunner,
) -> FirewallAssessment:
    if configuration.mode != "lan":
        return FirewallAssessment(
            "INFO",
            f"Not applicable to loopback-bound {configuration.mode} mode",
        )
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LANG": "C"})
    result = runner.run(
        ["ufw", "status"],
        check=False,
        capture=True,
        env=environment,
    )
    combined = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    if result.returncode == 127:
        return FirewallAssessment(
            "INFO",
            "UFW is not installed; firewall policy was not determined",
        )
    if result.returncode != 0:
        return FirewallAssessment(
            "UNKNOWN",
            (
                "UFW policy could not be read without elevation"
                + (f": {combined}" if combined else "")
            ),
        )
    return assess_ufw_status(combined, configuration)


def doctor(paths: InstallPaths, runner: CommandRunner) -> tuple[int, list[DoctorCheck]]:
    checks: list[DoctorCheck] = []
    server_configuration: ServerConfiguration | None = None

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
        try:
            server_configuration = read_server_configuration(paths.config)
            record(
                "PASS",
                "Network access mode",
                (
                    f"{server_configuration.mode} "
                    f"({server_configuration.host}:{server_configuration.port})"
                ),
            )
            cookie_value = server_configuration.cookie_secure
            cookie_policy = (
                str(cookie_value).lower()
                if not isinstance(cookie_value, bool)
                else "secure"
                if cookie_value
                else "insecure"
            )
            cookie_secure = (
                server_configuration.mode == "proxy"
                if cookie_policy == "auto"
                else cookie_policy in {"secure", "true"}
            )
            cookie_status = (
                "WARN"
                if server_configuration.mode == "lan" and not cookie_secure
                else "ERROR"
                if server_configuration.mode == "proxy" and not cookie_secure
                else "PASS"
            )
            cookie_detail = (
                f"{cookie_policy}; resolved Secure={'true' if cookie_secure else 'false'}"
            )
            if cookie_status == "WARN":
                cookie_detail += "; LAN HTTP is not encrypted"
            record(cookie_status, "Session Cookie policy", cookie_detail)
            urls = access_urls(server_configuration)
            origin_status = (
                "WARN"
                if server_configuration.mode in {"lan", "proxy"} and not urls
                else "PASS"
            )
            record(
                origin_status,
                "Trusted browser Origins",
                ", ".join(urls) if urls else "no external access URL configured",
            )
            proxy_status = (
                "WARN"
                if server_configuration.mode == "proxy"
                and not server_configuration.trusted_proxies
                else "PASS"
            )
            record(
                proxy_status,
                "Trusted proxy CIDRs",
                (
                    ", ".join(server_configuration.trusted_proxies)
                    if server_configuration.trusted_proxies
                    else "none; forwarding headers are ignored"
                ),
            )
            external_networks = [
                value
                for value in server_configuration.trusted_networks
                if value not in {"127.0.0.0/8", "::1/128"}
            ]
            network_status = (
                "WARN"
                if server_configuration.mode == "lan" and not external_networks
                else "PASS"
            )
            record(
                network_status,
                "Trusted network CIDRs",
                (
                    ", ".join(server_configuration.trusted_networks)
                    if server_configuration.trusted_networks
                    else "none; trusted networks never bypass authentication"
                ),
            )
            firewall = firewall_doctor_check(server_configuration, runner)
            record(firewall.status, "Firewall policy", firewall.detail)
        except DeployError as exc:
            record("ERROR", "Network configuration", str(exc))
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
    if server_configuration is not None and paths.unit.is_file():
        try:
            unit_content = paths.unit.read_text(encoding="utf-8")
            expected_host = f"--host {_systemd_quote(server_configuration.host)}"
            expected_port = f"--port {server_configuration.port}"
            unit_matches = expected_host in unit_content and expected_port in unit_content
            record(
                "PASS" if unit_matches else "ERROR",
                "Service binding consistency",
                (
                    f"{server_configuration.host}:{server_configuration.port}"
                    if unit_matches
                    else "service unit does not match config.toml"
                ),
            )
        except OSError as exc:
            record("ERROR", "Service binding consistency", str(exc))
    try:
        health_port = server_configuration.port if server_configuration else 8080
        with urllib.request.urlopen(f"http://127.0.0.1:{health_port}/health", timeout=2) as response:
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
    if any(check.status in {"WARN", "UNKNOWN"} for check in checks):
        return DOCTOR_WARNINGS, checks
    return DOCTOR_HEALTHY, checks


def _source_default() -> Path:
    return Path(__file__).resolve().parents[1]


def access_urls(configuration: ServerConfiguration) -> tuple[str, ...]:
    if configuration.mode == "lan":
        return tuple(
            origin
            for origin in configuration.trusted_origins
            if origin.startswith("http://")
            and "127.0.0.1" not in origin
            and "localhost" not in origin
        )
    if configuration.mode == "proxy":
        return tuple(
            origin
            for origin in configuration.trusted_origins
            if origin.startswith("https://")
            and "127.0.0.1" not in origin
            and "localhost" not in origin
        )
    return (f"http://127.0.0.1:{configuration.port}",)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="machinedeck-deploy")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--from-local", type=Path, default=_source_default())
    install_parser.add_argument("--enable-linger", action="store_true")
    install_parser.add_argument("--no-start", action="store_true")
    install_parser.add_argument("--yes", action="store_true")
    install_parser.add_argument(
        "--access",
        choices=sorted(INSTALL_ACCESS_MODES),
        default="local",
    )
    install_parser.add_argument(
        "--trusted-origin",
        action="append",
        default=[],
        help="Additional exact browser Origin; HTTPS is required for tailscale access",
    )
    install_parser.add_argument(
        "--trusted-proxy",
        action="append",
        default=[],
        help="Canonical proxy CIDR allowed to supply forwarding headers",
    )
    install_parser.add_argument(
        "--trusted-network",
        action="append",
        default=[],
        help="Canonical LAN CIDR for diagnostics and policy; does not bypass login",
    )
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
                access_mode=arguments.access,
                trusted_origins=arguments.trusted_origin,
                trusted_proxies=arguments.trusted_proxy,
                trusted_networks=arguments.trusted_network,
            )
            print(f"MachineDeck installed: {release}")
            installed_server = read_server_configuration(paths.config)
            urls = access_urls(installed_server)
            if urls:
                for url in urls:
                    print(f"Access URL: {url}")
            elif arguments.access == "tailscale":
                print(
                    "Tailscale proxy mode installed. Add its exact HTTPS Origin "
                    "with --trusted-origin or in config.toml before remote login."
                )
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
