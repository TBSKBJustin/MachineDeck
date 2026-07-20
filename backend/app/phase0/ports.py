from __future__ import annotations

import psutil

from .models import ProbeResult


def read_listening_ports() -> ProbeResult:
    """Discover listening TCP/UDP endpoints visible to the current user."""
    try:
        listeners = []
        for connection in psutil.net_connections(kind="inet"):
            if connection.type == 1 and connection.status != psutil.CONN_LISTEN:
                continue
            if not connection.laddr:
                continue
            listeners.append(
                {
                    "address": connection.laddr.ip,
                    "port": connection.laddr.port,
                    "protocol": "tcp" if connection.type == 1 else "udp",
                    "pid": connection.pid,
                }
            )
        listeners.sort(key=lambda item: (item["port"], item["protocol"]))
        return ProbeResult("listening_ports", True, data={"listeners": listeners})
    except (psutil.Error, OSError) as exc:
        return ProbeResult("listening_ports", False, error=str(exc))

