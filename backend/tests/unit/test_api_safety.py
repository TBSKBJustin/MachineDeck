from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.phase0.websocket_app import app


@pytest.mark.asyncio
async def test_lifecycle_api_rejects_arbitrary_request_arguments() -> None:
    transport = httpx.ASGITransport(app=app)
    mocked = AsyncMock()
    with patch("app.phase0.service_manager._run", mocked):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/phase0/applications/example-service/start",
                json={"command": "systemctl start ssh.service", "unit": "ssh.service"},
            )
    assert response.status_code == 400
    assert "do not accept" in response.json()["detail"]
    mocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_unregistered_application_never_reaches_a_subprocess() -> None:
    transport = httpx.ASGITransport(app=app)
    mocked = AsyncMock()
    with patch("app.phase0.service_manager._run", mocked):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/phase0/applications/not-registered/start")
    assert response.status_code == 404
    mocked.assert_not_awaited()
