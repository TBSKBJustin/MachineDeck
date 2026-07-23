from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_toml_configuration_is_loaded_and_environment_has_precedence(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[server]
mode = "proxy"
host = "127.0.0.2"
port = 9090
trusted_origins = ["https://machine.example.ts.net"]

[security]
cookie_secure = "auto"

[network]
trusted_proxies = ["127.0.0.1/32", "::1/128"]
trusted_networks = ["127.0.0.0/8", "192.168.1.0/24"]

[state]
database_url = "sqlite:////tmp/configured.db"

[paths]
allowed_roots = ["/tmp/configured-root"]
monitor_disks = ["/tmp"]
user_unit_dir = "/tmp/configured-units"
""".strip()
    )
    environment = os.environ.copy()
    environment.update(
        {
            "MACHINEDECK_CONFIG": str(config),
            "MACHINEDECK_BIND_PORT": "9191",
        }
    )
    command = (
        "import json; from app.config import settings; "
        "print(json.dumps({'host': settings.bind_host, 'port': settings.bind_port, "
        "'mode': settings.access_mode, 'cookie_secure': settings.auth_cookie_secure, "
        "'cookie_policy': settings.auth_cookie_secure_policy, "
        "'proxies': [str(value) for value in settings.trusted_proxies], "
        "'networks': [str(value) for value in settings.trusted_networks], "
        "'database': settings.database_url, 'origins': settings.trusted_origins, "
        "'roots': [str(path) for path in settings.allowed_roots]}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = json.loads(result.stdout)
    assert loaded == {
        "host": "127.0.0.2",
        "port": 9191,
        "mode": "proxy",
        "cookie_secure": True,
        "cookie_policy": "auto",
        "proxies": ["127.0.0.1/32", "::1/128"],
        "networks": ["127.0.0.0/8", "192.168.1.0/24"],
        "database": "sqlite:////tmp/configured.db",
        "origins": ["https://machine.example.ts.net"],
        "roots": ["/tmp/configured-root"],
    }


def test_lan_mode_auto_cookie_is_non_secure_and_requires_unspecified_bind(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[server]
mode = "lan"
host = "0.0.0.0"
port = 8080
trusted_origins = ["http://192.168.1.50:8080"]

[security]
cookie_secure = "auto"
""".strip()
    )
    environment = os.environ.copy()
    environment["MACHINEDECK_CONFIG"] = str(config)
    command = (
        "import json; from app.config import settings; "
        "print(json.dumps({'mode': settings.access_mode, 'host': settings.bind_host, "
        "'secure': settings.auth_cookie_secure}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "mode": "lan",
        "host": "0.0.0.0",
        "secure": False,
    }


def test_inconsistent_access_mode_and_bind_address_fail_closed(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[server]
mode = "proxy"
host = "0.0.0.0"
""".strip()
    )
    environment = os.environ.copy()
    environment["MACHINEDECK_CONFIG"] = str(config)
    result = subprocess.run(
        [sys.executable, "-c", "from app.config import settings"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "requires a loopback host" in result.stderr


def test_noncanonical_trusted_network_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[server]
mode = "lan"
host = "0.0.0.0"

[network]
trusted_networks = ["192.168.1.50/24"]
""".strip()
    )
    environment = os.environ.copy()
    environment["MACHINEDECK_CONFIG"] = str(config)
    result = subprocess.run(
        [sys.executable, "-c", "from app.config import settings"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "canonical IPv4 or IPv6 CIDRs" in result.stderr
