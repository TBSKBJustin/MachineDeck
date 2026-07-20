#!/usr/bin/env python3
"""Host acceptance for multi-service Docker Compose log attribution."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.adapters.runtime import DockerComposeAdapter, run_command  # noqa: E402
from app.logging.adapters import DockerComposeLogAdapter  # noqa: E402
from app.schemas.applications import ApplicationManifest  # noqa: E402


async def validate() -> int:
    fixture = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "compose-logs"
    manifest = ApplicationManifest.model_validate(
        {
            "id": "phase1-compose-log-fixture",
            "name": "Phase 1 Compose Log Fixture",
            "runtime": {
                "type": "compose",
                "working_dir": str(fixture),
                "compose_file": "compose.yaml",
                "project_name": "machinedeck-phase1-logs",
            },
        }
    )
    runtime = DockerComposeAdapter(manifest.id, manifest.runtime)
    logs = DockerComposeLogAdapter(manifest)
    output: dict[str, object] = {}
    exit_code = 1
    try:
        started = await runtime.start()
        output["start"] = started.succeeded
        if not started.succeeded:
            output["error"] = started.message
            return 1
        await asyncio.sleep(2)
        history = await logs.history(limit=20)
        output["history"] = [event.model_dump(mode="json") for event in history]
        services = {event.service for event in history}

        stream = logs.follow()
        first_live = await asyncio.wait_for(stream.__anext__(), timeout=5)
        await stream.aclose()
        await asyncio.sleep(0.2)
        output["first_live"] = first_live.model_dump(mode="json")
        output["active_reader_processes"] = len(logs.active_processes)
        exit_code = 0 if services >= {"api", "worker"} and not logs.active_processes else 1
    finally:
        await runtime.stop()
        removed = await run_command(
            [
                "docker",
                "compose",
                "-f",
                manifest.runtime.compose_file,
                "--project-name",
                manifest.runtime.project_name,
                "down",
            ],
            cwd=manifest.runtime.working_dir,
            timeout=120,
        )
        output["cleanup"] = removed.succeeded
        print(json.dumps(output, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(validate()))
