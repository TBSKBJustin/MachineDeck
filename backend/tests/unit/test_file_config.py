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
host = "127.0.0.2"
port = 9090
trusted_origins = ["https://machine.example.ts.net"]

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
        "database": "sqlite:////tmp/configured.db",
        "origins": ["https://machine.example.ts.net"],
        "roots": ["/tmp/configured-root"],
    }
