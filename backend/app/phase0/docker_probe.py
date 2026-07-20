from __future__ import annotations

from .models import ProbeResult


def read_docker_containers() -> ProbeResult:
    """Read containers and published ports without changing Docker state."""
    try:
        import docker
    except ImportError:
        return ProbeResult("docker", False, error="docker SDK is not installed")

    try:
        client = docker.from_env()
        try:
            version = client.version()
            containers = []
            for container in client.containers.list(all=True):
                ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
                containers.append(
                    {
                        "id": container.short_id,
                        "name": container.name,
                        "status": container.status,
                        "ports": ports,
                    }
                )
            return ProbeResult(
                "docker",
                True,
                data={"server_version": version.get("Version"), "containers": containers},
            )
        finally:
            client.close()
    except Exception as exc:  # Docker SDK uses several transport exception types.
        return ProbeResult("docker", False, error=str(exc))

