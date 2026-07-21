from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(os.getenv("MACHINEDECK_PROJECT_ROOT", BACKEND_ROOT.parent)).expanduser().resolve()


def _configuration() -> dict[str, Any]:
    configured = os.getenv("MACHINEDECK_CONFIG")
    if not configured:
        return {}
    path = Path(configured).expanduser()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"Cannot read MachineDeck configuration: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"MachineDeck configuration must be a TOML table: {path}")
    return data


CONFIG = _configuration()


def _value(section: str, name: str, default: Any) -> Any:
    table = CONFIG.get(section, {})
    return table.get(name, default) if isinstance(table, dict) else default


def _allowed_roots() -> tuple[Path, ...]:
    configured = os.getenv("MACHINEDECK_ALLOWED_ROOTS")
    values = (
        configured.split(os.pathsep)
        if configured
        else _value("paths", "allowed_roots", [str(PROJECT_ROOT.parent)])
    )
    if not isinstance(values, list):
        values = [values]
    return tuple(Path(value).expanduser().resolve() for value in values if value)


def _trusted_origins() -> tuple[str, ...]:
    configured = os.getenv("MACHINEDECK_TRUSTED_ORIGINS")
    values = configured.split(",") if configured else _value(
        "server",
        "trusted_origins",
        [
            "http://127.0.0.1:8080",
            "http://localhost:8080",
            "https://127.0.0.1:8080",
            "https://localhost:8080",
        ],
    )
    if not isinstance(values, list):
        values = [values]
    return tuple(value.strip().rstrip("/") for value in values if value.strip())


def _monitor_disks() -> tuple[Path, ...]:
    configured = os.getenv("MACHINEDECK_MONITOR_DISKS")
    values = configured.split(os.pathsep) if configured else _value(
        "paths",
        "monitor_disks",
        ["/", str(Path.home()), str(PROJECT_ROOT.parent.parent)],
    )
    if not isinstance(values, list):
        values = [values]
    return tuple(dict.fromkeys(Path(value).expanduser() for value in values if value))


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "MACHINEDECK_DATABASE_URL",
        _value("state", "database_url", f"sqlite:///{BACKEND_ROOT / 'data' / 'machinedeck.db'}"),
    )
    allowed_roots: tuple[Path, ...] = _allowed_roots()
    bind_host: str = os.getenv("MACHINEDECK_BIND_HOST", _value("server", "host", "127.0.0.1"))
    bind_port: int = int(os.getenv("MACHINEDECK_BIND_PORT", _value("server", "port", 8080)))
    user_unit_dir: Path = Path(
        os.getenv(
            "MACHINEDECK_USER_UNIT_DIR",
            _value(
                "paths",
                "user_unit_dir",
                str(Path.home() / ".config" / "systemd" / "user"),
            ),
        )
    ).expanduser()
    allow_privileged_ports: bool = os.getenv(
        "MACHINEDECK_ALLOW_PRIVILEGED_PORTS", "false"
    ).lower() in {"1", "true", "yes"}
    public_host_local: str = os.getenv(
        "MACHINEDECK_PUBLIC_HOST_LOCAL", _value("server", "public_host_local", "127.0.0.1")
    )
    public_host_lan: str | None = os.getenv(
        "MACHINEDECK_PUBLIC_HOST_LAN", _value("server", "public_host_lan", None)
    )
    auth_cookie_name: str = "machinedeck_session"
    auth_cookie_secure: bool = True
    auth_session_hours: int = int(os.getenv("MACHINEDECK_AUTH_SESSION_HOURS", "12"))
    trusted_origins: tuple[str, ...] = _trusted_origins()
    login_max_failures: int = int(os.getenv("MACHINEDECK_LOGIN_MAX_FAILURES", "5"))
    login_window_minutes: int = int(os.getenv("MACHINEDECK_LOGIN_WINDOW_MINUTES", "15"))
    dashboard_interval_seconds: float = float(
        os.getenv("MACHINEDECK_DASHBOARD_INTERVAL_SECONDS", "2")
    )
    monitor_disks: tuple[Path, ...] = _monitor_disks()


settings = Settings()
