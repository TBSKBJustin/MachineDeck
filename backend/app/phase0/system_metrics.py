from __future__ import annotations

import shutil

import psutil

from .models import ProbeResult


def read_system_metrics() -> ProbeResult:
    """Read a small, JSON-safe snapshot of CPU, memory, and disk usage."""
    try:
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return ProbeResult(
            name="system_metrics",
            available=True,
            data={
                "cpu": {
                    "percent": psutil.cpu_percent(interval=None),
                    "logical_cores": psutil.cpu_count(logical=True),
                    "physical_cores": psutil.cpu_count(logical=False),
                    "load_average": list(psutil.getloadavg()),
                },
                "memory": {
                    "total_bytes": memory.total,
                    "available_bytes": memory.available,
                    "used_bytes": memory.used,
                    "percent": memory.percent,
                },
                "disk": {
                    "path": "/",
                    "total_bytes": disk.total,
                    "used_bytes": disk.used,
                    "free_bytes": disk.free,
                    "percent": disk.percent,
                },
            },
        )
    except (OSError, RuntimeError) as exc:
        return ProbeResult("system_metrics", False, error=str(exc))


def permission_snapshot() -> ProbeResult:
    """Report tool presence only; this probe never elevates privileges."""
    commands = ("systemctl", "journalctl", "docker", "nvidia-smi")
    paths = {command: shutil.which(command) for command in commands}
    missing = [command for command, path in paths.items() if path is None]
    return ProbeResult(
        name="permissions",
        available=not missing,
        data={"commands": paths},
        warnings=[f"Command not found: {command}" for command in missing],
    )

