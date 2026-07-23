from __future__ import annotations

import os
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
ACCESS_MODES = {"local", "lan", "proxy"}
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _value(section: str, name: str, default: Any) -> Any:
    table = CONFIG.get(section, {})
    return table.get(name, default) if isinstance(table, dict) else default


def _access_mode() -> str:
    configured_host = str(_value("server", "host", "127.0.0.1")).strip()
    inferred = "lan" if configured_host in {"0.0.0.0", "::"} else "local"
    value = os.getenv("MACHINEDECK_ACCESS_MODE", _value("server", "mode", inferred))
    normalized = str(value).strip().lower()
    if normalized not in ACCESS_MODES:
        raise RuntimeError(
            "server.mode must be one of: local, lan, proxy"
        )
    return normalized


def _bind_host(access_mode: str) -> str:
    default = "0.0.0.0" if access_mode == "lan" else "127.0.0.1"
    value = str(
        os.getenv("MACHINEDECK_BIND_HOST", _value("server", "host", default))
    ).strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise RuntimeError("server.host must be a literal IP address") from exc
    if access_mode in {"local", "proxy"} and not address.is_loopback:
        raise RuntimeError(f"server.mode={access_mode} requires a loopback host")
    if access_mode == "lan" and not address.is_unspecified:
        raise RuntimeError("server.mode=lan requires host 0.0.0.0 or ::")
    return str(address)


def _bind_port() -> int:
    value = os.getenv("MACHINEDECK_BIND_PORT", _value("server", "port", 8080))
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("server.port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("server.port must be between 1 and 65535")
    return port


def _cookie_security(access_mode: str) -> tuple[str, bool]:
    legacy_default: str | bool = (
        True if CONFIG and "security" not in CONFIG else "auto"
    )
    value = os.getenv(
        "MACHINEDECK_COOKIE_SECURE",
        _value("security", "cookie_secure", legacy_default),
    )
    if isinstance(value, bool):
        return ("secure" if value else "insecure"), value
    normalized = str(value).strip().lower()
    aliases = {
        "true": "secure",
        "yes": "secure",
        "1": "secure",
        "false": "insecure",
        "no": "insecure",
        "0": "insecure",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"auto", "secure", "insecure"}:
        raise RuntimeError(
            "security.cookie_secure must be auto, secure, insecure, true, or false"
        )
    return normalized, access_mode == "proxy" if normalized == "auto" else normalized == "secure"


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
    origins: list[str] = []
    for value in values:
        origin = str(value).strip().rstrip("/")
        if not origin:
            continue
        try:
            parsed = urlsplit(origin)
            _ = parsed.port
        except ValueError as exc:
            raise RuntimeError("server.trusted_origins contains an invalid Origin") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "*" in origin
        ):
            raise RuntimeError(
                "server.trusted_origins must contain exact HTTP or HTTPS Origins"
            )
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


def _networks(name: str, default: list[str]) -> tuple[IPNetwork, ...]:
    environment_name = f"MACHINEDECK_{name.upper()}"
    configured = os.getenv(environment_name)
    values = (
        configured.split(",")
        if configured
        else _value("network", name, default)
    )
    if not isinstance(values, list):
        values = [values]
    networks: list[IPNetwork] = []
    for value in values:
        try:
            network = ipaddress.ip_network(str(value).strip(), strict=True)
        except ValueError as exc:
            raise RuntimeError(
                f"network.{name} must contain canonical IPv4 or IPv6 CIDRs"
            ) from exc
        if network not in networks:
            networks.append(network)
        if name == "trusted_proxies" and network.num_addresses > 65536:
            raise RuntimeError(
                "network.trusted_proxies entries cannot contain more than 65536 addresses"
            )
    return tuple(networks)


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


ACCESS_MODE = _access_mode()
COOKIE_SECURE_POLICY, COOKIE_SECURE = _cookie_security(ACCESS_MODE)
TRUSTED_PROXIES = _networks("trusted_proxies", [])
TRUSTED_NETWORKS = _networks(
    "trusted_networks", ["127.0.0.0/8", "::1/128"]
)


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "MACHINEDECK_DATABASE_URL",
        _value("state", "database_url", f"sqlite:///{BACKEND_ROOT / 'data' / 'machinedeck.db'}"),
    )
    allowed_roots: tuple[Path, ...] = _allowed_roots()
    access_mode: str = ACCESS_MODE
    bind_host: str = _bind_host(ACCESS_MODE)
    bind_port: int = _bind_port()
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
    auth_cookie_secure_policy: str = COOKIE_SECURE_POLICY
    auth_cookie_secure: bool = COOKIE_SECURE
    auth_session_hours: int = int(os.getenv("MACHINEDECK_AUTH_SESSION_HOURS", "12"))
    trusted_origins: tuple[str, ...] = _trusted_origins()
    trusted_proxies: tuple[IPNetwork, ...] = TRUSTED_PROXIES
    trusted_networks: tuple[IPNetwork, ...] = TRUSTED_NETWORKS
    forwarded_hop_limit: int = 8
    login_max_failures: int = int(os.getenv("MACHINEDECK_LOGIN_MAX_FAILURES", "5"))
    login_window_minutes: int = int(os.getenv("MACHINEDECK_LOGIN_WINDOW_MINUTES", "15"))
    dashboard_interval_seconds: float = float(
        os.getenv("MACHINEDECK_DASHBOARD_INTERVAL_SECONDS", "2")
    )
    monitor_disks: tuple[Path, ...] = _monitor_disks()


settings = Settings()
