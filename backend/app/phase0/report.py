from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .docker_probe import read_docker_containers
from .gpu import read_gpu_metrics
from .ports import read_listening_ports
from .system_metrics import permission_snapshot, read_system_metrics


def collect_report() -> dict[str, Any]:
    probes = (
        read_system_metrics(),
        read_gpu_metrics(),
        read_docker_containers(),
        read_listening_ports(),
        permission_snapshot(),
    )
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": 0,
        "probes": {probe.name: probe.to_dict() for probe in probes},
    }

