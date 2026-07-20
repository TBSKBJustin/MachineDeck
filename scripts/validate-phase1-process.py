#!/usr/bin/env python3
"""Host acceptance check for the Phase 1 managed process lifecycle."""

from __future__ import annotations

import asyncio
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.adapters.runtime import UserSystemdAdapter, run_command  # noqa: E402
from app.schemas.applications import ApplicationManifest  # noqa: E402
from app.systemd.user_units import UserUnitManager  # noqa: E402


async def cleanup_fixture(
    manager: UserUnitManager, adapter: UserSystemdAdapter
) -> dict[str, object]:
    stopped = await adapter.stop()
    target = manager.target_path(adapter.application_id)
    removed = False
    if target.exists():
        target.unlink()
        removed = True
    reloaded = await run_command(["systemctl", "--user", "daemon-reload"], timeout=30)
    reset = await run_command(
        ["systemctl", "--user", "reset-failed", adapter.unit], timeout=30
    )
    confirmed = await run_command(
        [
            "systemctl",
            "--user",
            "show",
            adapter.unit,
            "--property=LoadState",
            "--no-pager",
        ],
        timeout=30,
    )
    not_found = "LoadState=not-found" in confirmed.message or not confirmed.succeeded
    reset_succeeded = reset.succeeded or "not loaded" in reset.message.lower()
    return {
        "stop": asdict(stopped),
        "removed": removed,
        "daemon_reload": asdict(reloaded),
        "reset_failed": {
            **asdict(reset),
            "succeeded": reset_succeeded,
        },
        "unit_not_found": not_found,
    }


async def validate(cleanup: bool = False) -> int:
    fixture = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "process"
    manifest = ApplicationManifest.model_validate(
        {
            "id": "phase1-process-fixture",
            "name": "Phase 1 Process Fixture",
            "runtime": {
                "type": "process",
                "working_dir": str(fixture),
                "command": [str(fixture / "example.sh")],
            },
            "environment": {"MACHINEDECK_FIXTURE": "phase1"},
        }
    )
    manager = UserUnitManager()
    adapter = UserSystemdAdapter(manifest, manager)
    output: dict[str, object] = {}
    exit_code = 1
    try:
        started = await adapter.start()
        output["start"] = asdict(started)
        runtime_state = await adapter.status()
        output["runtime_state"] = {
            "status": runtime_state.status.value,
            "runtime_identifier": runtime_state.runtime_identifier,
            "metadata": runtime_state.metadata,
            "error_message": runtime_state.error_message,
        }
        consistency = manager.consistency(manifest)
        output["unit_consistency"] = {
            "status": consistency.status.value,
            "unit_name": consistency.unit_name,
            "message": consistency.message,
        }
        exit_code = 0 if started.succeeded and runtime_state.status.value == "RUNNING" else 1
    finally:
        if cleanup:
            cleanup_result = await cleanup_fixture(manager, adapter)
            output["cleanup"] = cleanup_result
            if not cleanup_result["daemon_reload"]["succeeded"] or not cleanup_result[
                "unit_not_found"
            ]:
                exit_code = 1
        else:
            stopped = await adapter.stop()
            output["stop"] = asdict(stopped)
        print(json.dumps(output, indent=2))
    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Stop and remove the fixture unit after acceptance validation.",
    )
    raise SystemExit(asyncio.run(validate(cleanup=parser.parse_args().cleanup)))
