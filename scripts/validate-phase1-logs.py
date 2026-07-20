#!/usr/bin/env python3
"""Host acceptance for process history/follow WebSocket and reader cleanup."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
temporary_directory = tempfile.TemporaryDirectory(prefix="machinedeck-log-acceptance-")
os.environ["MACHINEDECK_DATABASE_URL"] = (
    f"sqlite:///{Path(temporary_directory.name) / 'acceptance.db'}"
)
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import psutil  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.adapters.runtime import UserSystemdAdapter, run_command  # noqa: E402
from app.database.session import SessionLocal, create_schema  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas.applications import ApplicationManifest  # noqa: E402
from app.services.applications import create_application  # noqa: E402
from app.systemd.user_units import UserUnitManager  # noqa: E402


def journal_follow_processes(unit: str) -> list[int]:
    matches = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            command = process.info["cmdline"] or []
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if command and "journalctl" in Path(command[0]).name and "--follow" in command and unit in command:
            matches.append(process.info["pid"])
    return matches


async def remove_fixture(adapter: UserSystemdAdapter, manager: UserUnitManager) -> None:
    await adapter.stop()
    target = manager.target_path(adapter.application_id)
    target.unlink(missing_ok=True)
    await run_command(["systemctl", "--user", "daemon-reload"], timeout=30)
    await run_command(["systemctl", "--user", "reset-failed", adapter.unit], timeout=30)


def main() -> int:
    import asyncio

    fixture = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "process"
    manifest = ApplicationManifest.model_validate(
        {
            "id": "phase1-log-fixture",
            "name": "Phase 1 Log Fixture",
            "runtime": {
                "type": "process",
                "working_dir": str(fixture),
                "command": [str(fixture / "example.sh")],
            },
        }
    )
    manager = UserUnitManager()
    adapter = UserSystemdAdapter(manifest, manager)
    output: dict[str, object] = {}
    exit_code = 1
    create_schema()
    with SessionLocal() as session:
        create_application(session, manifest)
    try:
        started = asyncio.run(adapter.start())
        output["start"] = started.succeeded
        if not started.succeeded:
            output["error"] = started.message
            return 1
        time.sleep(2)
        with TestClient(app) as client:
            with client.websocket_connect(
                f"/ws/v1/applications/{manifest.id}/logs?history=1&follow=true"
            ) as websocket:
                connected = websocket.receive_json()
                history = websocket.receive_json()
                followed = websocket.receive_json()
                output["connected"] = connected
                output["history"] = history
                output["follow"] = followed
        time.sleep(0.3)
        residual = journal_follow_processes(adapter.unit)
        output["residual_journalctl_pids"] = residual
        exit_code = 0 if (
            connected.get("type") == "status"
            and history.get("type") == "log"
            and followed.get("type") == "log"
            and history["data"].get("cursor") != followed["data"].get("cursor")
            and not residual
        ) else 1
    finally:
        asyncio.run(remove_fixture(adapter, manager))
        temporary_directory.cleanup()
        print(json.dumps(output, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
