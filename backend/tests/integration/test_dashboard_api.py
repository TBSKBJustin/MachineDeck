from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import QueryParams

from app.api.dashboard import dashboard_websocket
from app.database.base import Base
from app.database.session import get_session
from app.main import app
from app.schemas.dashboard import (
    ApplicationSummary,
    CollectorState,
    DashboardSnapshot,
    Freshness,
    HostMetrics,
)
from app.security.auth import require_http_auth


def snapshot() -> DashboardSnapshot:
    return DashboardSnapshot(
        collected_at=datetime.now(timezone.utc),
        collection_duration_ms=42,
        freshness=Freshness.LIVE,
        host=HostMetrics(cpu_percent=25, swap_total_bytes=0),
        gpus=[],
        applications=ApplicationSummary(total=3, running=1, stopped=2),
        collectors={
            "host": CollectorState(available=True),
            "nvml": CollectorState(
                available=False,
                error_code="NVML_NOT_AVAILABLE",
                message="IMPORTERROR",
            ),
        },
    )


@pytest.fixture
def database_override() -> Generator[None, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    async def override() -> AsyncGenerator[Session, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.asyncio
async def test_dashboard_requires_auth_and_partial_collectors_still_return_200(
    database_override: None,
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        unauthorized = await client.get("/api/v1/dashboard")

        async def bypass_auth() -> None:
            return None

        app.dependency_overrides[require_http_auth] = bypass_auth
        with patch(
            "app.api.dashboard.metrics_service.latest",
            AsyncMock(return_value=snapshot()),
        ):
            response = await client.get("/api/v1/dashboard")
    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["freshness"] == "LIVE"
    assert response.json()["collectors"]["nvml"]["available"] is False


class FakeWebSocket:
    def __init__(self, query: str = "") -> None:
        self.query_params = QueryParams(query)
        self.accepted = False
        self.sent = []
        self.closed = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, value: dict) -> None:
        self.sent.append(value)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class FakeMetricsService:
    async def subscribe(self) -> AsyncGenerator[DashboardSnapshot, None]:
        yield snapshot()


async def never_expires(_: str) -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_dashboard_websocket_sends_snapshot_immediately() -> None:
    websocket = FakeWebSocket()
    with (
        patch(
            "app.api.dashboard.authenticate_websocket",
            AsyncMock(return_value="session-id"),
        ),
        patch("app.api.dashboard.wait_for_websocket_session_end", never_expires),
        patch("app.api.dashboard.metrics_service", FakeMetricsService()),
    ):
        await dashboard_websocket(websocket)
    assert websocket.accepted
    assert websocket.sent[0]["type"] == "dashboard_snapshot"
    assert websocket.sent[0]["data"]["applications"]["total"] == 3
    assert websocket.closed[0] == 1012


@pytest.mark.asyncio
async def test_dashboard_websocket_rejects_query_tokens_before_accept() -> None:
    websocket = FakeWebSocket("token=secret")
    with patch(
        "app.api.dashboard.authenticate_websocket",
        AsyncMock(return_value="session-id"),
    ):
        await dashboard_websocket(websocket)
    assert not websocket.accepted
    assert websocket.closed[0] == 4400


@pytest.mark.asyncio
async def test_frontend_assets_exist_and_http_has_strict_browser_security_headers() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    frontend = Path(__file__).resolve().parents[3] / "frontend"
    markup = (frontend / "index.html").read_text(encoding="utf-8")
    assert "MachineDeck" in markup
    assert "Audit log" in markup
    assert 'id="audit-execution-id"' in markup
    assert 'id="application-list"' in markup
    assert 'id="application-form"' in markup
    assert 'id="application-runtime"' in markup
    assert 'id="port-list"' in markup
    assert 'id="log-output"' in markup
    script = (frontend / "app.js").read_text(encoding="utf-8")
    assert '"/api/v1/applications/validate"' in script
    assert "/ws/v1/applications/" in script
    assert "data-application-action" in script
    assert "query string" not in script.lower()
    assert (frontend / "styles.css").is_file()
