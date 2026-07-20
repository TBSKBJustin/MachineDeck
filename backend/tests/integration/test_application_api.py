from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import AuditEventRecord
from app.database.session import get_session
from app.main import app
from app.schemas.ports import ObservedPort
from app.security.auth import require_http_auth


@pytest.fixture
def session_factory() -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: object, _: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    async def override_session() -> AsyncGenerator[Session, None]:
        with factory() as session:
            yield session

    async def bypass_auth() -> None:
        return None

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_http_auth] = bypass_auth
    try:
        yield factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def manifest() -> dict:
    project = Path(__file__).resolve().parents[1] / "fixtures" / "compose"
    return {
        "version": 1,
        "id": "fixture-stack",
        "name": "Fixture Stack",
        "description": "Phase 1 integration fixture",
        "runtime": {
            "type": "compose",
            "working_dir": str(project),
            "compose_file": "compose.yaml",
            "project_name": "fixture-stack",
        },
        "ports": [{"id": "web", "name": "Web UI", "protocol": "http", "host": 8080}],
        "tags": ["test"],
    }


@pytest.mark.asyncio
async def test_application_crud_is_persistent_and_audited(
    session_factory: sessionmaker[Session],
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/v1/applications", json=manifest())
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "STOPPED"

        duplicate = await client.post("/api/v1/applications", json=manifest())
        assert duplicate.status_code == 409

        listed = await client.get("/api/v1/applications")
        assert [item["id"] for item in listed.json()] == ["fixture-stack"]

        updated_manifest = manifest()
        updated_manifest["name"] = "Updated Fixture"
        updated = await client.put("/api/v1/applications/fixture-stack", json=updated_manifest)
        assert updated.status_code == 200
        assert updated.json()["name"] == "Updated Fixture"

        validated = await client.post("/api/v1/applications/fixture-stack/validate")
        assert validated.status_code == 200
        assert validated.json()["valid"] is True

        deleted = await client.delete("/api/v1/applications/fixture-stack")
        assert deleted.status_code == 204
        missing = await client.get("/api/v1/applications/fixture-stack")
        assert missing.status_code == 404

    with session_factory() as session:
        count = session.scalar(select(func.count()).select_from(AuditEventRecord))
        actions = session.scalars(select(AuditEventRecord.action).order_by(AuditEventRecord.created_at)).all()
    assert count == 3
    assert actions == ["application.create", "application.update", "application.delete"]


@pytest.mark.asyncio
async def test_invalid_manifest_is_not_persisted(session_factory: sessionmaker[Session]) -> None:
    invalid = manifest()
    invalid["runtime"]["compose_file"] = "../compose.yaml"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/applications", json=invalid)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_port_and_endpoint_routes_return_safe_declared_runtime_views(
    session_factory: sessionmaker[Session],
) -> None:
    saved_manifest = manifest()
    saved_manifest["ports"] = [
        {
            "id": "web",
            "name": "Web UI",
            "protocol": "http",
            "host_port": 18080,
            "bind_address": "0.0.0.0",
            "path": "/ui?mode=full",
            "primary": True,
            "open_in_browser": True,
        }
    ]
    observed = ObservedPort(
        bind_address="0.0.0.0",
        host_port=18080,
        protocol="tcp",
        source="compose",
        service="web",
        application_id="fixture-stack",
    )
    transport = httpx.ASGITransport(app=app)
    with (
        patch(
            "app.orchestration.port_discovery.RuntimePortDiscovery.discover",
            AsyncMock(return_value=[observed]),
        ),
        patch(
            "app.orchestration.ports.scan_host_listeners",
            AsyncMock(return_value=[]),
        ),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/api/v1/applications", json=saved_manifest)).status_code == 201
            ports = await client.get(
                "/api/v1/applications/fixture-stack/ports",
                headers={"host": "attacker.example"},
            )
            endpoints = await client.get(
                "/api/v1/applications/fixture-stack/endpoints",
                headers={"host": "attacker.example"},
            )
            refreshed = await client.post(
                "/api/v1/applications/fixture-stack/ports/refresh"
            )
            rejected_body = await client.post(
                "/api/v1/applications/fixture-stack/ports/refresh",
                json={"command": "unsafe"},
            )
            system_ports = await client.get("/api/v1/system/ports")

    assert ports.status_code == 200
    assert ports.json()["ports"][0]["status"] == "LISTENING"
    assert ports.json()["ports"][0]["declared"]["host_port"] == 18080
    assert endpoints.status_code == 200
    assert endpoints.json()["primary"]["url"] == "http://127.0.0.1:18080/ui?mode=full"
    assert "attacker.example" not in endpoints.text
    assert refreshed.status_code == 200
    assert rejected_body.status_code == 400
    assert system_ports.status_code == 200
