from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from pathlib import Path

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

    app.dependency_overrides[get_session] = override_session
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
