#!/usr/bin/env python3
"""Host acceptance for process/Compose port discovery and conflict checks."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.adapters.runtime import DockerComposeAdapter, UserSystemdAdapter, run_command  # noqa: E402
from app.database.base import Base  # noqa: E402
from app.orchestration.port_discovery import ComposePortDiscovery, ProcessPortDiscovery  # noqa: E402
from app.orchestration.ports import PortService, endpoint_url  # noqa: E402
from app.schemas.applications import ApplicationManifest  # noqa: E402
from app.services.applications import create_application  # noqa: E402
from app.systemd.user_units import UserUnitManager  # noqa: E402


def unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def wait_for_ports(discovery: object, manifest: ApplicationManifest) -> list:
    for _ in range(30):
        observed = await discovery.discover(manifest)
        if observed:
            return observed
        await asyncio.sleep(0.2)
    return []


async def remove_process_fixture(
    manager: UserUnitManager, adapter: UserSystemdAdapter
) -> dict[str, object]:
    stopped = await adapter.stop()
    target = manager.target_path(adapter.application_id)
    removed = False
    if target.exists():
        target.unlink()
        removed = True
    reloaded = await run_command(["systemctl", "--user", "daemon-reload"], timeout=30)
    await run_command(["systemctl", "--user", "reset-failed", adapter.unit], timeout=30)
    return {"stop": asdict(stopped), "unit_removed": removed, "daemon_reload": asdict(reloaded)}


async def validate() -> int:
    process_fixture = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "process"
    compose_fixture = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "compose"
    process_port = unused_tcp_port()
    process_manifest = ApplicationManifest.model_validate(
        {
            "id": "phase1-port-process-fixture",
            "name": "Phase 1 Port Process Fixture",
            "runtime": {
                "type": "process",
                "working_dir": str(process_fixture),
                "command": [str(process_fixture / "port_server.py"), str(process_port)],
            },
            "ports": [
                {
                    "id": "web",
                    "name": "Web UI",
                    "protocol": "http",
                    "host_port": process_port,
                    "bind_address": "127.0.0.1",
                    "primary": True,
                    "open_in_browser": True,
                }
            ],
        }
    )
    compose_manifest = ApplicationManifest.model_validate(
        {
            "id": "phase1-port-compose-fixture",
            "name": "Phase 1 Port Compose Fixture",
            "runtime": {
                "type": "compose",
                "working_dir": str(compose_fixture),
                "compose_file": "compose.yaml",
                "project_name": "machinedeck-phase1-port-fixture",
            },
        }
    )
    manager = UserUnitManager()
    process_adapter = UserSystemdAdapter(process_manifest, manager)
    compose_adapter = DockerComposeAdapter(compose_manifest.id, compose_manifest.runtime)
    output: dict[str, object] = {}
    passed = False
    try:
        process_start = await process_adapter.start()
        process_observed = await wait_for_ports(ProcessPortDiscovery(), process_manifest)
        output["process"] = {
            "start": asdict(process_start),
            "observed": [item.model_dump(mode="json") for item in process_observed],
            "url": endpoint_url(process_manifest.ports[0]),
        }

        compose_start = await compose_adapter.start()
        compose_observed = await wait_for_ports(ComposePortDiscovery(), compose_manifest)
        output["compose"] = {
            "start": asdict(compose_start),
            "observed": [item.model_dump(mode="json") for item in compose_observed],
        }

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            conflict_port = int(occupied.getsockname()[1])
            conflict_manifest = ApplicationManifest.model_validate(
                {
                    "id": "phase1-port-conflict-fixture",
                    "name": "Phase 1 Port Conflict Fixture",
                    "runtime": {
                        "type": "process",
                        "working_dir": str(process_fixture),
                        "command": [str(process_fixture / "port_server.py"), str(conflict_port)],
                    },
                    "ports": [
                        {
                            "id": "web",
                            "name": "Occupied Web Port",
                            "host_port": conflict_port,
                            "bind_address": "127.0.0.1",
                        }
                    ],
                }
            )
            engine = create_engine("sqlite://")
            Base.metadata.create_all(engine)
            try:
                with Session(engine, expire_on_commit=False) as session:
                    create_application(session, conflict_manifest)
                    conflicts = await PortService(session).conflicts(conflict_manifest)
            finally:
                engine.dispose()
            output["conflict"] = [item.model_dump(mode="json") for item in conflicts]

        process_ok = any(
            item.host_port == process_port
            and item.bind_address == "127.0.0.1"
            and item.pid is not None
            for item in process_observed
        )
        compose_ok = any(
            item.source == "compose"
            and item.bind_address == "127.0.0.1"
            and item.host_port > 0
            and item.container_port == 80
            for item in compose_observed
        )
        conflict_ok = any(
            item.port == conflict_port
            and item.pid == os.getpid()
            and not item.managed_by_machinedeck
            for item in conflicts
        )
        passed = process_start.succeeded and compose_start.succeeded and process_ok and compose_ok and conflict_ok
    finally:
        output["process_cleanup"] = await remove_process_fixture(manager, process_adapter)
        compose_down = await run_command(
            compose_adapter.base_command + ["down", "--remove-orphans"],
            cwd=compose_manifest.runtime.working_dir,
            timeout=120,
        )
        output["compose_cleanup"] = asdict(compose_down)
        print(json.dumps(output, indent=2))
    return 0 if passed and output["process_cleanup"]["daemon_reload"]["succeeded"] and output["compose_cleanup"]["succeeded"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(validate()))
