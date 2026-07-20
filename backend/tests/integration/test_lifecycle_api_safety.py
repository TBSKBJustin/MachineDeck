from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.main import app
from app.security.auth import require_http_auth


@pytest.mark.asyncio
async def test_lifecycle_body_is_rejected_before_service_execution() -> None:
    mocked = AsyncMock()
    transport = httpx.ASGITransport(app=app)

    async def bypass_auth() -> None:
        return None

    app.dependency_overrides[require_http_auth] = bypass_auth
    try:
        with patch("app.orchestration.lifecycle_service.LifecycleService.action", mocked):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/applications/safe-app/start",
                    json={
                        "command": "systemctl start ssh.service",
                        "unit": "ssh.service",
                        "arguments": ["--no-block"],
                    },
                )
    finally:
        app.dependency_overrides.pop(require_http_auth, None)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "ARGUMENTS_NOT_ALLOWED"
    mocked.assert_not_awaited()
