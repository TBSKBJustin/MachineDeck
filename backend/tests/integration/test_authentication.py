from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database.base import Base
from app.database.models import (
    AdministratorRecord,
    AuditEventRecord,
    AuthSessionRecord,
    LoginFailureRecord,
)
from app.database.session import get_session
from app.main import app
from app.schemas.auth import Credentials


PASSWORD = "correct horse battery staple"
ORIGIN = "https://127.0.0.1:8080"


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


def credentials(password: str = PASSWORD) -> dict[str, str]:
    return {"username": "admin", "password": password}


@pytest.mark.asyncio
async def test_setup_creates_one_argon2id_admin_and_server_side_session(
    session_factory: sessionmaker[Session],
) -> None:
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://127.0.0.1:8080"
    ) as client:
        initial = await client.get("/api/v1/auth/status")
        setup = await client.post(
            "/api/v1/auth/setup", json=credentials(), headers={"origin": ORIGIN}
        )
        status_response = await client.get("/api/v1/auth/status")
        protected = await client.get("/api/v1/applications")
        duplicate = await client.post(
            "/api/v1/auth/setup", json=credentials(), headers={"origin": ORIGIN}
        )

    assert initial.json() == {"setup_required": True, "authenticated": False}
    assert setup.status_code == 201
    assert setup.json()["username"] == "admin"
    assert "password" not in setup.text
    cookie = setup.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert status_response.json() == {"setup_required": False, "authenticated": True}
    assert protected.status_code == 200
    assert duplicate.status_code == 409

    with session_factory() as session:
        admin = session.scalar(select(AdministratorRecord))
        saved_session = session.scalar(select(AuthSessionRecord))
        assert admin.password_hash.startswith("$argon2id$")
        assert PASSWORD not in admin.password_hash
        assert len(saved_session.token_digest) == 64
        assert setup.json()["csrf_token"] not in {
            saved_session.token_digest,
            saved_session.csrf_digest,
        }


@pytest.mark.asyncio
async def test_csrf_is_required_and_logout_revokes_the_session(
    session_factory: sessionmaker[Session],
) -> None:
    project = Path(__file__).resolve().parents[1] / "fixtures" / "compose"
    manifest = {
        "id": "auth-app",
        "name": "Authenticated App",
        "runtime": {"type": "compose", "working_dir": str(project)},
    }
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://127.0.0.1:8080"
    ) as client:
        setup = await client.post(
            "/api/v1/auth/setup", json=credentials(), headers={"origin": ORIGIN}
        )
        csrf = setup.json()["csrf_token"]
        missing = await client.post("/api/v1/applications", json=manifest)
        hostile = await client.post(
            "/api/v1/applications",
            json=manifest,
            headers={"origin": "https://attacker.example", "x-csrf-token": csrf},
        )
        created = await client.post(
            "/api/v1/applications",
            json=manifest,
            headers={"origin": ORIGIN, "x-csrf-token": csrf},
        )
        logged_out = await client.post(
            "/api/v1/auth/logout",
            headers={"origin": ORIGIN, "x-csrf-token": csrf},
        )
        after_logout = await client.get("/api/v1/applications")

    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "CSRF_INVALID"
    assert hostile.status_code == 403
    assert hostile.json()["detail"]["code"] == "ORIGIN_NOT_ALLOWED"
    assert created.status_code == 201
    assert logged_out.status_code == 204
    assert after_logout.status_code == 401
    with session_factory() as session:
        assert session.scalar(select(AuthSessionRecord)).revoked_at is not None
        registry_event = session.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.action == "application.create"
            )
        )
        assert registry_event.actor == "admin"
        assert registry_event.details_json["request"] == {
            "method": "POST",
            "path": "/api/v1/applications",
        }
        actions = session.scalars(
            select(AuditEventRecord.action).where(AuditEventRecord.action.like("auth.%"))
        ).all()
        assert set(actions) == {"auth.setup", "auth.logout"}


@pytest.mark.asyncio
async def test_login_failures_are_rate_limited_and_audited(
    session_factory: sessionmaker[Session],
) -> None:
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    limited_settings = replace(settings, login_max_failures=2)
    with patch("app.security.auth.settings", limited_settings):
        async with httpx.AsyncClient(
            transport=transport, base_url="https://127.0.0.1:8080"
        ) as client:
            setup = await client.post(
                "/api/v1/auth/setup", json=credentials(), headers={"origin": ORIGIN}
            )
            csrf = setup.json()["csrf_token"]
            await client.post(
                "/api/v1/auth/logout",
                headers={"origin": ORIGIN, "x-csrf-token": csrf},
            )
            first = await client.post(
                "/api/v1/auth/login",
                json=credentials("incorrect password one"),
                headers={"origin": ORIGIN},
            )
            second = await client.post(
                "/api/v1/auth/login",
                json=credentials("incorrect password two"),
                headers={"origin": ORIGIN},
            )
            limited = await client.post(
                "/api/v1/auth/login", json=credentials(), headers={"origin": ORIGIN}
            )

    assert first.status_code == second.status_code == 401
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "900"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(LoginFailureRecord)) == 2
        failures = session.scalar(
            select(func.count()).select_from(AuditEventRecord).where(
                AuditEventRecord.action == "auth.login",
                AuditEventRecord.result == "failure",
            )
        )
        assert failures == 3


@pytest.mark.asyncio
async def test_setup_rejects_remote_clients_and_untrusted_origins(
    session_factory: sessionmaker[Session],
) -> None:
    remote_transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 12345))
    async with httpx.AsyncClient(
        transport=remote_transport, base_url="https://127.0.0.1:8080"
    ) as client:
        remote = await client.post(
            "/api/v1/auth/setup", json=credentials(), headers={"origin": ORIGIN}
        )
    local_transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(
        transport=local_transport, base_url="https://127.0.0.1:8080"
    ) as client:
        hostile = await client.post(
            "/api/v1/auth/setup",
            json=credentials(),
            headers={"origin": "https://attacker.example"},
        )
    assert remote.status_code == 403
    assert remote.json()["detail"]["code"] == "LOCAL_SETUP_REQUIRED"
    assert "127.0.0.1" in remote.json()["detail"]["message"]
    assert hostile.status_code == 403
    assert hostile.json()["detail"]["code"] == "ORIGIN_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_remote_setup_reports_local_requirement_before_origin_configuration(
    session_factory: sessionmaker[Session],
) -> None:
    transport = httpx.ASGITransport(app=app, client=("100.101.88.36", 56821))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://100.100.100.100:8080"
    ) as client:
        response = await client.post(
            "/api/v1/auth/setup",
            json=credentials(),
            headers={"origin": "http://100.100.100.100:8080"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "LOCAL_SETUP_REQUIRED"


@pytest.mark.asyncio
async def test_auth_validation_does_not_echo_password(
    session_factory: sessionmaker[Session],
) -> None:
    submitted = "7chars!"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://127.0.0.1:8080"
    ) as client:
        response = await client.post(
            "/api/v1/auth/setup",
            json=credentials(submitted),
            headers={"origin": ORIGIN},
        )
    assert response.status_code == 422
    assert submitted not in response.text


def test_password_minimum_is_eight_characters() -> None:
    assert Credentials(username="admin", password="12345678").password == "12345678"
    with pytest.raises(ValidationError):
        Credentials(username="admin", password="1234567")
