from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.database.models import AuditEventRecord
from app.database.session import get_session
from app.main import app
from app.security.auth import require_http_auth


@pytest.fixture
def audit_database() -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                AuditEventRecord(
                    id="00000000-0000-0000-0000-000000000001",
                    actor="anonymous",
                    action="auth.login",
                    target_type="authentication",
                    target_id="administrator",
                    result="failure",
                    details_json={
                        "reason": "invalid_credentials",
                        "password": "api-must-not-return-this",
                    },
                    created_at=datetime(2026, 7, 20, 20, 0, tzinfo=timezone.utc),
                ),
                AuditEventRecord(
                    id="00000000-0000-0000-0000-000000000002",
                    actor="phase1-api",
                    action="application.stop",
                    target_type="application",
                    target_id="comfyui",
                    result="success",
                    details_json={},
                    created_at=datetime(2026, 7, 20, 21, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        session.commit()

    async def override_session() -> AsyncGenerator[Session, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.asyncio
async def test_audit_api_requires_auth_filters_and_never_returns_password(
    audit_database: sessionmaker[Session],
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        unauthorized = await client.get("/api/v1/audit-events")

        async def bypass_auth() -> None:
            return None

        app.dependency_overrides[require_http_auth] = bypass_auth
        page = await client.get(
            "/api/v1/audit-events",
            params={"category": "authentication", "result": "failure"},
        )
        detail = await client.get(
            "/api/v1/audit-events/00000000-0000-0000-0000-000000000001"
        )
    assert unauthorized.status_code == 401
    assert page.status_code == 200
    assert page.json()["events"][0]["action"] == "LOGIN_FAILED"
    assert detail.status_code == 200
    assert "api-must-not-return-this" not in page.text + detail.text
    assert detail.json()["details"]["password"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_audit_api_rejects_invalid_filters_and_missing_events(
    audit_database: sessionmaker[Session],
) -> None:
    async def bypass_auth() -> None:
        return None

    app.dependency_overrides[require_http_auth] = bypass_auth
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        bad_result = await client.get("/api/v1/audit-events?result=maybe")
        bad_category = await client.get("/api/v1/audit-events?category=not-real")
        bad_cursor = await client.get("/api/v1/audit-events?cursor=broken")
        bad_time = await client.get(
            "/api/v1/audit-events?start=2026-07-21T00:00:00Z&end=2026-07-20T00:00:00Z"
        )
        missing = await client.get(
            "/api/v1/audit-events/00000000-0000-0000-0000-999999999999"
        )
    assert bad_result.status_code == 422
    assert bad_category.status_code == 422
    assert bad_cursor.status_code == 400
    assert bad_time.status_code == 400
    assert missing.status_code == 404
