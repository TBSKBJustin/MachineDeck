from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.log_websocket import application_logs
from app.database.base import Base
from app.database.models import AuthSessionRecord
from app.main import system_metrics
from app.security.auth import (
    authenticate_websocket,
    create_administrator,
    create_auth_session,
    utc_now,
    wait_for_websocket_session_end,
)


class FakeWebSocket:
    def __init__(
        self,
        *,
        origin: str | None = "https://127.0.0.1:8080",
        token: str | None = None,
    ) -> None:
        self.headers = {"origin": origin} if origin else {}
        self.cookies = {"machinedeck_session": token} if token else {}
        self.accepted = False
        self.closed: tuple[int, str] | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


@pytest.mark.asyncio
async def test_websocket_rejects_origin_and_session_before_accept() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with patch("app.security.auth.SessionLocal", factory):
        hostile = FakeWebSocket(origin="https://attacker.example")
        anonymous = FakeWebSocket()
        assert not await authenticate_websocket(hostile)
        assert not await authenticate_websocket(anonymous)
    assert not hostile.accepted and hostile.closed[0] == 4403
    assert not anonymous.accepted and anonymous.closed[0] == 4401
    engine.dispose()


@pytest.mark.asyncio
async def test_valid_websocket_session_and_expiration() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        admin = create_administrator(session, "admin", "correct horse battery staple")
        saved, token, _ = create_auth_session(session, admin)
    with patch("app.security.auth.SessionLocal", factory):
        assert await authenticate_websocket(FakeWebSocket(token=token))
        with factory() as session:
            current = session.scalar(select(AuthSessionRecord).where(AuthSessionRecord.id == saved.id))
            current.expires_at = utc_now() - timedelta(seconds=1)
            session.commit()
        expired = FakeWebSocket(token=token)
        assert not await authenticate_websocket(expired)
        assert expired.closed[0] == 4401
    engine.dispose()


@pytest.mark.asyncio
async def test_established_websocket_guard_detects_revocation() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        admin = create_administrator(session, "admin", "correct horse battery staple")
        saved, _, _ = create_auth_session(session, admin)
        saved.revoked_at = utc_now()
        session.commit()
    with patch("app.security.auth.SessionLocal", factory):
        await asyncio.wait_for(
            wait_for_websocket_session_end(saved.id, interval_seconds=0.001),
            timeout=1,
        )
    engine.dispose()


@pytest.mark.asyncio
async def test_log_and_metric_handlers_do_not_accept_before_authentication() -> None:
    log_socket = FakeWebSocket()
    metric_socket = FakeWebSocket()
    with patch(
        "app.api.log_websocket.authenticate_websocket", AsyncMock(return_value=None)
    ):
        await application_logs(log_socket, "private-application")
    with patch("app.main.authenticate_websocket", AsyncMock(return_value=None)):
        await system_metrics(metric_socket)
    assert not log_socket.accepted
    assert not metric_socket.accepted
