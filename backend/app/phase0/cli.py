from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path

from .report import collect_report
from .registry import ApplicationRegistry, RegistryError
from .service_manager import LifecycleRouter


DEFAULT_REGISTRY = Path(__file__).resolve().parents[2] / "config" / "phase0-applications.yaml"


async def _lifecycle_command(app_id: str, action: str, lines: int) -> int:
    try:
        registry = ApplicationRegistry.load(DEFAULT_REGISTRY)
        manager = LifecycleRouter(registry).for_application(app_id)
        if action == "logs":
            print(
                json.dumps(
                    {"application_id": app_id, "lines": await manager.logs(app_id, lines)}, indent=2
                )
            )
            return 0
        result = await getattr(manager, action)(app_id)
        print(json.dumps(asdict(result), indent=2))
        return 0 if result.succeeded else 1
    except (RegistryError, ValueError) as exc:
        print(json.dumps({"succeeded": False, "error": str(exc)}, indent=2))
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="MachineDeck Phase 0 capability validation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("report", help="Run all read-only host probes")

    lifecycle = subparsers.add_parser("app", help="Operate an application from the trusted registry")
    lifecycle.add_argument("application_id")
    lifecycle.add_argument("action", choices=("start", "stop", "restart", "status", "logs"))
    lifecycle.add_argument("--lines", type=int, default=50)
    args = parser.parse_args()

    if args.command == "report":
        print(json.dumps(collect_report(), indent=2))
        return 0
    return asyncio.run(_lifecycle_command(args.application_id, args.action, args.lines))


if __name__ == "__main__":
    raise SystemExit(main())
