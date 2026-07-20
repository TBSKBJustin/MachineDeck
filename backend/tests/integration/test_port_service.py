from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database.base import Base
from app.database.models import ApplicationInstanceRecord
from app.orchestration.port_discovery import PortDiscoveryError
from app.orchestration.ports import PortService
from app.schemas.applications import ApplicationManifest
from app.schemas.ports import ObservedPort, PortStatus
from app.services.applications import create_application


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "compose"


def manifest(application_id: str, port: int = 8188, protocol: str = "http") -> ApplicationManifest:
    return ApplicationManifest.model_validate(
        {
            "id": application_id,
            "name": application_id,
            "runtime": {"type": "compose", "working_dir": str(FIXTURE)},
            "ports": [
                {
                    "id": "web" if protocol == "http" else "raw",
                    "name": "Endpoint",
                    "protocol": protocol,
                    "host_port": port,
                    "bind_address": "0.0.0.0",
                    "path": "/" if protocol == "http" else None,
                    "primary": protocol == "http",
                    "open_in_browser": protocol == "http",
                }
            ],
        }
    )


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as database_session:
        yield database_session
    engine.dispose()


class FakeDiscovery:
    def __init__(self, values: dict[str, list[ObservedPort]]) -> None:
        self.values = values

    async def discover(self, saved_manifest: ApplicationManifest) -> list[ObservedPort]:
        return [item.model_copy(deep=True) for item in self.values.get(saved_manifest.id, [])]


@pytest.mark.asyncio
async def test_declared_and_observed_ports_remain_separate(session: Session) -> None:
    saved = manifest("observed-app")
    create_application(session, saved)
    discovery = FakeDiscovery(
        {
            saved.id: [
                ObservedPort(
                    bind_address="0.0.0.0",
                    host_port=8188,
                    protocol="tcp",
                    source="compose",
                    service="web",
                    application_id=saved.id,
                ),
                ObservedPort(
                    bind_address="127.0.0.1",
                    host_port=9999,
                    protocol="tcp",
                    source="compose",
                    service="metrics",
                    application_id=saved.id,
                ),
            ]
        }
    )
    with patch("app.orchestration.ports.scan_host_listeners", AsyncMock(return_value=[])):
        response = await PortService(session, discovery).ports(saved.id)
    assert response.ports[0].declared.host_port == 8188
    assert response.ports[0].observed.service == "web"
    assert response.ports[0].status == PortStatus.LISTENING
    assert response.ports[0].url == "http://127.0.0.1:8188/"
    assert response.ports[1].declared is None
    assert response.ports[1].status == PortStatus.UNDECLARED


@pytest.mark.asyncio
async def test_running_declared_port_without_listener_is_not_listening(session: Session) -> None:
    saved = manifest("missing-listener")
    create_application(session, saved)
    state = session.scalar(
        select(ApplicationInstanceRecord).where(
            ApplicationInstanceRecord.application_id == saved.id
        )
    )
    state.status = "RUNNING"
    session.commit()
    with patch("app.orchestration.ports.scan_host_listeners", AsyncMock(return_value=[])):
        response = await PortService(session, FakeDiscovery({})).ports(saved.id)
    assert response.ports[0].status == PortStatus.NOT_LISTENING
    assert response.ports[0].url == "http://127.0.0.1:8188/"


@pytest.mark.asyncio
async def test_listener_on_different_specific_address_remains_undeclared(session: Session) -> None:
    saved = manifest("address-mismatch")
    saved.ports[0].bind_address = "127.0.0.1"
    create_application(session, saved)
    observed = ObservedPort(
        bind_address="192.168.1.50",
        host_port=8188,
        protocol="tcp",
        source="process",
        application_id=saved.id,
    )
    with patch("app.orchestration.ports.scan_host_listeners", AsyncMock(return_value=[])):
        response = await PortService(
            session, FakeDiscovery({saved.id: [observed]})
        ).ports(saved.id)
    assert response.ports[0].status == PortStatus.DECLARED
    assert response.ports[0].observed is None
    assert response.ports[1].status == PortStatus.UNDECLARED


@pytest.mark.asyncio
async def test_unavailable_system_scan_degrades_port_status_to_unknown(session: Session) -> None:
    saved = manifest("scan-unavailable")
    create_application(session, saved)
    with patch(
        "app.orchestration.ports.scan_host_listeners",
        AsyncMock(side_effect=PortDiscoveryError("permission denied")),
    ):
        response = await PortService(session, FakeDiscovery({})).ports(saved.id)
    assert response.ports[0].status == PortStatus.UNKNOWN


@pytest.mark.asyncio
async def test_external_wildcard_listener_is_a_structured_conflict(session: Session) -> None:
    saved = manifest("conflicted-app")
    create_application(session, saved)
    listener = ObservedPort(
        bind_address="127.0.0.1",
        host_port=8188,
        protocol="tcp",
        pid=123,
        process_name="python",
        source="system",
    )
    with patch(
        "app.orchestration.ports.scan_host_listeners", AsyncMock(return_value=[listener])
    ):
        conflicts = await PortService(session, FakeDiscovery({})).conflicts(saved)
    assert len(conflicts) == 1
    assert conflicts[0].pid == 123
    assert not conflicts[0].managed_by_machinedeck


@pytest.mark.asyncio
async def test_tcp_and_udp_same_port_do_not_conflict(session: Session) -> None:
    saved = manifest("tcp-app", protocol="tcp")
    create_application(session, saved)
    udp = ObservedPort(
        bind_address="0.0.0.0", host_port=8188, protocol="udp", source="system"
    )
    with patch("app.orchestration.ports.scan_host_listeners", AsyncMock(return_value=[udp])):
        assert await PortService(session, FakeDiscovery({})).conflicts(saved) == []


@pytest.mark.asyncio
async def test_own_listener_is_not_reported_but_other_managed_app_is(session: Session) -> None:
    first = manifest("first-app")
    second = manifest("second-app")
    create_application(session, first)
    create_application(session, second)
    first_observed = ObservedPort(
        bind_address="0.0.0.0",
        host_port=8188,
        protocol="tcp",
        pid=111,
        source="process",
        application_id=first.id,
    )
    listener = first_observed.model_copy(update={"source": "system", "application_id": None})
    discovery = FakeDiscovery({first.id: [first_observed]})
    with patch(
        "app.orchestration.ports.scan_host_listeners", AsyncMock(return_value=[listener])
    ):
        service = PortService(session, discovery)
        assert await service.conflicts(first) == []
        conflicts = await service.conflicts(second)
    assert conflicts[0].application_id == first.id
    assert conflicts[0].managed_by_machinedeck
