from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import psutil
import pytest

from app.adapters.runtime import AdapterResult
from app.orchestration.port_discovery import (
    ComposePortDiscovery,
    PortDiscoveryError,
    ProcessPortDiscovery,
)
from app.schemas.applications import ApplicationManifest


FIXTURE_PROCESS = Path(__file__).resolve().parents[1] / "fixtures" / "process"
FIXTURE_COMPOSE = Path(__file__).resolve().parents[1] / "fixtures" / "compose"


def connection(pid: int, port: int, *, protocol: str = "tcp", address: str = "127.0.0.1") -> SimpleNamespace:
    return SimpleNamespace(
        pid=pid,
        laddr=SimpleNamespace(ip=address, port=port),
        type=socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM,
        status=psutil.CONN_LISTEN if protocol == "tcp" else psutil.CONN_NONE,
    )


def process_manifest() -> ApplicationManifest:
    return ApplicationManifest.model_validate(
        {
            "id": "process-ports",
            "name": "Process Ports",
            "runtime": {
                "type": "process",
                "working_dir": str(FIXTURE_PROCESS),
                "command": [str(FIXTURE_PROCESS / "example.sh")],
            },
        }
    )


@pytest.mark.asyncio
async def test_process_discovery_includes_systemd_child_process_listener() -> None:
    discovery = ProcessPortDiscovery(connection_provider=lambda **_: [connection(222, 8188)])
    with (
        patch(
            "app.orchestration.port_discovery.run_command",
            AsyncMock(
                return_value=AdapterResult(
                    True, "MainPID=111\nControlGroup=/user.slice/test.service"
                )
            ),
        ),
        patch.object(discovery, "_read_cgroup_pids", return_value={111, 222}),
        patch("app.orchestration.port_discovery.psutil.Process") as process,
    ):
        process.return_value.name.return_value = "python"
        observed = await discovery.discover(process_manifest())
    assert len(observed) == 1
    assert observed[0].pid == 222
    assert observed[0].host_port == 8188
    assert observed[0].application_id == "process-ports"


@pytest.mark.asyncio
async def test_process_discovery_survives_pid_exit_during_name_lookup() -> None:
    discovery = ProcessPortDiscovery(connection_provider=lambda **_: [connection(111, 8188)])
    with (
        patch(
            "app.orchestration.port_discovery.run_command",
            AsyncMock(return_value=AdapterResult(True, "MainPID=111\nControlGroup=")),
        ),
        patch(
            "app.orchestration.port_discovery.psutil.Process",
            side_effect=psutil.NoSuchProcess(111),
        ),
    ):
        observed = await discovery.discover(process_manifest())
    assert observed[0].process_name is None


@pytest.mark.asyncio
async def test_process_discovery_reports_permission_failure() -> None:
    def denied(**_: object) -> list:
        raise psutil.AccessDenied()

    discovery = ProcessPortDiscovery(connection_provider=denied)
    with patch(
        "app.orchestration.port_discovery.run_command",
        AsyncMock(return_value=AdapterResult(True, "MainPID=111\nControlGroup=")),
    ):
        with pytest.raises(PortDiscoveryError):
            await discovery.discover(process_manifest())


class FakeContainer:
    short_id = "container-id"
    attrs = {
        "Config": {
            "Labels": {
                "com.docker.compose.project": "compose",
                "com.docker.compose.project.working_dir": str(FIXTURE_COMPOSE),
                "com.docker.compose.service": "web",
            }
        },
        "NetworkSettings": {
            "Ports": {
                "80/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": "32768"},
                    {"HostIp": "::", "HostPort": "32769"},
                ],
                "53/udp": [{"HostIp": "0.0.0.0", "HostPort": "5353"}],
                "443/tcp": None,
            }
        },
    }


class FakeDockerClient:
    def __init__(self) -> None:
        self.containers = SimpleNamespace(list=lambda **_: [FakeContainer()])
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_compose_discovery_reads_only_published_dynamic_host_ports() -> None:
    client = FakeDockerClient()
    manifest = ApplicationManifest.model_validate(
        {
            "id": "compose-ports",
            "name": "Compose Ports",
            "runtime": {
                "type": "compose",
                "working_dir": str(FIXTURE_COMPOSE),
                "compose_file": "compose.yaml",
            },
        }
    )
    observed = await ComposePortDiscovery(lambda: client).discover(manifest)
    assert {(item.bind_address, item.host_port, item.protocol) for item in observed} == {
        ("127.0.0.1", 32768, "tcp"),
        ("::", 32769, "tcp"),
        ("0.0.0.0", 5353, "udp"),
    }
    assert all(item.service == "web" and item.container_id == "container-id" for item in observed)
    assert client.closed


@pytest.mark.asyncio
async def test_compose_discovery_falls_back_to_safe_docker_cli_inspection() -> None:
    manifest = ApplicationManifest.model_validate(
        {
            "id": "compose-cli-ports",
            "name": "Compose CLI Ports",
            "runtime": {
                "type": "compose",
                "working_dir": str(FIXTURE_COMPOSE),
                "project_name": "compose-cli-ports",
            },
        }
    )
    inspected = {
        **FakeContainer.attrs,
        "Id": "1234567890abcdef",
        "Config": {
            "Labels": {
                **FakeContainer.attrs["Config"]["Labels"],
                "com.docker.compose.project": "compose-cli-ports",
            }
        },
    }
    # An injected factory keeps this unit test synchronous; production SDK work
    # is deliberately offloaded from the event loop.
    discovery = ComposePortDiscovery(lambda: None)
    with (
        patch.object(discovery, "_discover_sync", side_effect=RuntimeError("broken SDK")),
        patch(
            "app.orchestration.port_discovery.run_command",
            AsyncMock(
                side_effect=[
                    AdapterResult(True, "1234567890ab"),
                    AdapterResult(True, json.dumps([inspected])),
                ]
            ),
        ) as command,
    ):
        observed = await discovery.discover(manifest)
    assert any(item.host_port == 32768 and item.container_port == 80 for item in observed)
    assert observed[0].container_id == "1234567890ab"
    assert command.await_args_list[0].args[0][:3] == ["docker", "ps", "--all"]
