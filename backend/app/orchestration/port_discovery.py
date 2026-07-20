from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from pathlib import Path, PurePosixPath
from typing import Callable

import psutil

from app.adapters.runtime import run_command
from app.schemas.applications import ApplicationManifest, ComposeRuntime, ProcessRuntime
from app.schemas.ports import ObservedPort
from app.systemd.user_units import unit_name_for


class PortDiscoveryError(RuntimeError):
    pass


def _normalize_address(value: str) -> str:
    if value in {"", "*"}:
        return "0.0.0.0"
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value


class ProcessPortDiscovery:
    def __init__(
        self,
        connection_provider: Callable[..., list] = psutil.net_connections,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ) -> None:
        self.connection_provider = connection_provider
        self.cgroup_root = cgroup_root

    async def _unit_processes(self, application_id: str) -> set[int]:
        unit = unit_name_for(application_id)
        result = await run_command(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=MainPID,ControlGroup",
                "--no-pager",
            ]
        )
        if not result.succeeded:
            raise PortDiscoveryError(result.message or f"Cannot read process group for {unit}")
        values = {}
        for line in result.message.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        pids: set[int] = set()
        try:
            main_pid = int(values.get("MainPID", "0"))
        except ValueError:
            main_pid = 0
        if main_pid > 0:
            pids.add(main_pid)
        control_group = values.get("ControlGroup", "")
        if control_group:
            pids.update(self._read_cgroup_pids(control_group))
        if main_pid > 0 and len(pids) == 1:
            try:
                process = psutil.Process(main_pid)
                pids.update(child.pid for child in process.children(recursive=True))
            except (psutil.Error, OSError):
                pass
        return pids

    def _read_cgroup_pids(self, control_group: str) -> set[int]:
        pure = PurePosixPath(control_group)
        if not pure.is_absolute() or ".." in pure.parts:
            raise PortDiscoveryError("systemd returned an unsafe control group path")
        root = self.cgroup_root.resolve()
        target = (root / str(pure).lstrip("/")).resolve()
        if target != root and not target.is_relative_to(root):
            raise PortDiscoveryError("Control group escaped the cgroup root")
        pids: set[int] = set()
        try:
            files = [target / "cgroup.procs", *target.glob("**/cgroup.procs")]
            for path in files:
                try:
                    for value in path.read_text(encoding="ascii").splitlines():
                        if value.isdigit():
                            pids.add(int(value))
                except (FileNotFoundError, PermissionError, OSError):
                    continue
        except (FileNotFoundError, PermissionError, OSError):
            return pids
        return pids

    async def discover(self, manifest: ApplicationManifest) -> list[ObservedPort]:
        if not isinstance(manifest.runtime, ProcessRuntime):
            raise ValueError("ProcessPortDiscovery requires a process manifest")
        pids = await self._unit_processes(manifest.id)
        if not pids:
            return []
        try:
            connections = (
                await asyncio.to_thread(self.connection_provider, kind="inet")
                if self.connection_provider is psutil.net_connections
                else self.connection_provider(kind="inet")
            )
        except (psutil.Error, OSError) as exc:
            raise PortDiscoveryError(str(exc)) from exc
        observed = []
        for connection in connections:
            if connection.pid not in pids or not connection.laddr:
                continue
            protocol = "tcp" if connection.type == socket.SOCK_STREAM else "udp"
            if protocol == "tcp" and connection.status != psutil.CONN_LISTEN:
                continue
            try:
                process_name = psutil.Process(connection.pid).name() if connection.pid else None
            except (psutil.Error, OSError):
                process_name = None
            observed.append(
                ObservedPort(
                    bind_address=_normalize_address(connection.laddr.ip),
                    host_port=connection.laddr.port,
                    protocol=protocol,
                    pid=connection.pid,
                    process_name=process_name,
                    source="process",
                    application_id=manifest.id,
                )
            )
        return observed


class ComposePortDiscovery:
    def __init__(self, docker_client_factory: Callable | None = None) -> None:
        self.docker_client_factory = docker_client_factory

    @staticmethod
    def _observed_from_containers(
        manifest: ApplicationManifest, containers: list[tuple[str, dict]]
    ) -> list[ObservedPort]:
        runtime = manifest.runtime
        if not isinstance(runtime, ComposeRuntime):
            raise ValueError("ComposePortDiscovery requires a Compose manifest")
        observed = []
        expected_project = runtime.project_name or runtime.working_dir.name
        for container_id, attributes in containers:
            labels = attributes.get("Config", {}).get("Labels", {}) or {}
            if labels.get("com.docker.compose.project") != expected_project:
                continue
            registered_dir = labels.get("com.docker.compose.project.working_dir")
            if registered_dir and Path(registered_dir).resolve() != runtime.working_dir:
                continue
            service = labels.get("com.docker.compose.service")
            ports = attributes.get("NetworkSettings", {}).get("Ports", {}) or {}
            for container_binding, host_bindings in ports.items():
                if not host_bindings:
                    continue
                container_port_text, _, protocol = container_binding.partition("/")
                if protocol not in {"tcp", "udp"} or not container_port_text.isdigit():
                    continue
                for binding in host_bindings:
                    host_port = str(binding.get("HostPort", ""))
                    if not host_port.isdigit():
                        continue
                    observed.append(
                        ObservedPort(
                            bind_address=_normalize_address(binding.get("HostIp", "")),
                            host_port=int(host_port),
                            protocol=protocol,
                            source="compose",
                            service=service,
                            container_id=container_id[:12],
                            container_port=int(container_port_text),
                            application_id=manifest.id,
                        )
                    )
        return observed

    def _discover_sync(self, manifest: ApplicationManifest) -> list[ObservedPort]:
        runtime = manifest.runtime
        if not isinstance(runtime, ComposeRuntime):
            raise ValueError("ComposePortDiscovery requires a Compose manifest")
        if self.docker_client_factory:
            client = self.docker_client_factory()
        else:
            import docker

            client = docker.from_env()
        project = runtime.project_name or runtime.working_dir.name
        try:
            docker_containers = client.containers.list(
                all=True, filters={"label": f"com.docker.compose.project={project}"}
            )
            containers = [(container.short_id, container.attrs) for container in docker_containers]
            return self._observed_from_containers(manifest, containers)
        finally:
            client.close()

    async def _discover_cli(self, manifest: ApplicationManifest) -> list[ObservedPort]:
        runtime = manifest.runtime
        if not isinstance(runtime, ComposeRuntime):
            raise ValueError("ComposePortDiscovery requires a Compose manifest")
        project = runtime.project_name or runtime.working_dir.name
        listed = await run_command(
            [
                "docker",
                "ps",
                "--all",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.ID}}",
            ]
        )
        if not listed.succeeded:
            raise PortDiscoveryError(listed.message or "Cannot list Compose containers")
        container_ids = [line for line in listed.message.splitlines() if line]
        if not container_ids:
            return []
        inspected = await run_command(["docker", "inspect", *container_ids])
        if not inspected.succeeded:
            raise PortDiscoveryError(inspected.message or "Cannot inspect Compose containers")
        try:
            attributes = json.loads(inspected.message)
        except json.JSONDecodeError as exc:
            raise PortDiscoveryError("Docker inspect returned invalid JSON") from exc
        if not isinstance(attributes, list):
            raise PortDiscoveryError("Docker inspect returned an unexpected response")
        containers = [
            (str(item.get("Id", "")), item) for item in attributes if isinstance(item, dict)
        ]
        return self._observed_from_containers(manifest, containers)

    async def discover(self, manifest: ApplicationManifest) -> list[ObservedPort]:
        sdk_error: Exception | None = None
        try:
            if self.docker_client_factory is not None:
                return self._discover_sync(manifest)
            return await asyncio.to_thread(self._discover_sync, manifest)
        except Exception as exc:
            sdk_error = exc
        try:
            return await self._discover_cli(manifest)
        except Exception as cli_error:
            raise PortDiscoveryError(
                f"Docker Engine discovery failed via SDK ({sdk_error}) and CLI ({cli_error})"
            ) from cli_error


class RuntimePortDiscovery:
    def __init__(
        self,
        process: ProcessPortDiscovery | None = None,
        compose: ComposePortDiscovery | None = None,
    ) -> None:
        self.process = process or ProcessPortDiscovery()
        self.compose = compose or ComposePortDiscovery()

    async def discover(self, manifest: ApplicationManifest) -> list[ObservedPort]:
        if isinstance(manifest.runtime, ProcessRuntime):
            return await self.process.discover(manifest)
        if isinstance(manifest.runtime, ComposeRuntime):
            return await self.compose.discover(manifest)
        return []


async def scan_host_listeners(
    connection_provider: Callable[..., list] = psutil.net_connections,
) -> list[ObservedPort]:
    try:
        connections = (
            await asyncio.to_thread(connection_provider, kind="inet")
            if connection_provider is psutil.net_connections
            else connection_provider(kind="inet")
        )
    except (psutil.Error, OSError) as exc:
        raise PortDiscoveryError(str(exc)) from exc
    observed = []
    for connection in connections:
        if not connection.laddr:
            continue
        protocol = "tcp" if connection.type == socket.SOCK_STREAM else "udp"
        if protocol == "tcp" and connection.status != psutil.CONN_LISTEN:
            continue
        try:
            process_name = psutil.Process(connection.pid).name() if connection.pid else None
        except (psutil.Error, OSError):
            process_name = None
        observed.append(
            ObservedPort(
                bind_address=_normalize_address(connection.laddr.ip),
                host_port=connection.laddr.port,
                protocol=protocol,
                pid=connection.pid,
                process_name=process_name,
                source="system",
            )
        )
    return observed
