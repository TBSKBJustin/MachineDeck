from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import urlsplit
from uuid import uuid4

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response, WebSocket, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database.models import (
    AdministratorRecord,
    AuditEventRecord,
    AuthSessionRecord,
    LoginFailureRecord,
)
from app.database.session import SessionLocal, get_session


password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
DUMMY_PASSWORD_HASH = password_hasher.hash("machinedeck-dummy-password-never-valid")
CSRF_HEADER = "X-CSRF-Token"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def remote_digest(host: str | None) -> str:
    return digest(host or "unknown")


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def origin_is_trusted(origin: str | None) -> bool:
    if origin is None:
        return False
    normalized = origin.rstrip("/")
    if normalized in settings.trusted_origins:
        return True
    try:
        parsed = urlsplit(origin)
        # Accessing port also validates that it is numeric and in range.
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and is_loopback(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def require_trusted_origin(origin: str | None, *, allow_missing: bool = True) -> None:
    if origin is None and allow_missing:
        return
    if not origin_is_trusted(origin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ORIGIN_NOT_ALLOWED", "message": "Request origin is not trusted"},
        )


@dataclass(frozen=True)
class AuthenticatedSession:
    administrator: AdministratorRecord
    session: AuthSessionRecord
    csrf_token: str | None = None


def administrator(session: Session) -> AdministratorRecord | None:
    return session.scalar(select(AdministratorRecord).limit(1))


def authenticate_token(session: Session, token: str | None) -> AuthenticatedSession | None:
    if not token:
        return None
    saved = session.scalar(select(AuthSessionRecord).where(AuthSessionRecord.token_digest == digest(token)))
    if saved is None or saved.revoked_at is not None or _aware(saved.expires_at) <= utc_now():
        return None
    admin = session.get(AdministratorRecord, saved.administrator_id)
    return AuthenticatedSession(admin, saved) if admin else None


def _audit(
    session: Session,
    *,
    actor: str,
    action: str,
    result: str,
    target_id: str = "administrator",
    details: dict | None = None,
) -> None:
    session.add(
        AuditEventRecord(
            id=str(uuid4()),
            actor=actor,
            action=action,
            target_type="authentication",
            target_id=target_id,
            result=result,
            details_json=details or {},
        )
    )


def create_administrator(session: Session, username: str, password: str) -> AdministratorRecord:
    if administrator(session) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SETUP_COMPLETE", "message": "Administrator setup is already complete"},
        )
    saved = AdministratorRecord(
        id=str(uuid4()),
        singleton_key=1,
        username=username,
        password_hash=password_hasher.hash(password),
    )
    session.add(saved)
    _audit(session, actor=username, action="auth.setup", result="success", target_id=saved.id)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SETUP_COMPLETE", "message": "Administrator setup is already complete"},
        ) from exc
    return saved


def create_auth_session(
    session: Session, admin: AdministratorRecord
) -> tuple[AuthSessionRecord, str, str]:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    saved = AuthSessionRecord(
        id=str(uuid4()),
        administrator_id=admin.id,
        token_digest=digest(token),
        csrf_digest=digest(csrf_token),
        expires_at=utc_now() + timedelta(hours=settings.auth_session_hours),
    )
    session.add(saved)
    session.commit()
    return saved, token, csrf_token


def rotate_csrf(session: Session, saved: AuthSessionRecord) -> str:
    csrf_token = secrets.token_urlsafe(32)
    saved.csrf_digest = digest(csrf_token)
    session.commit()
    return csrf_token


def set_session_cookie(response: Response, token: str, expires_at: datetime) -> None:
    response.set_cookie(
        settings.auth_cookie_name,
        token,
        max_age=max(0, int((_aware(expires_at) - utc_now()).total_seconds())),
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.auth_cookie_name,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )


def rate_limited(session: Session, remote: str) -> bool:
    cutoff = utc_now() - timedelta(minutes=settings.login_window_minutes)
    key = remote_digest(remote)
    session.execute(delete(LoginFailureRecord).where(LoginFailureRecord.attempted_at < cutoff))
    session.commit()
    count = session.scalar(
        select(func.count()).select_from(LoginFailureRecord).where(
            LoginFailureRecord.remote_digest == key,
            LoginFailureRecord.attempted_at >= cutoff,
        )
    )
    return int(count or 0) >= settings.login_max_failures


def record_login_failure(session: Session, remote: str, reason: str) -> None:
    session.add(LoginFailureRecord(id=str(uuid4()), remote_digest=remote_digest(remote)))
    _audit(
        session,
        actor="anonymous",
        action="auth.login",
        result="failure",
        details={"reason": reason},
    )
    session.commit()


def clear_login_failures(session: Session, remote: str) -> None:
    session.execute(
        delete(LoginFailureRecord).where(LoginFailureRecord.remote_digest == remote_digest(remote))
    )
    session.commit()


def verify_credentials(
    session: Session, username: str, password: str
) -> AdministratorRecord | None:
    admin = administrator(session)
    candidate_hash = admin.password_hash if admin and hmac.compare_digest(admin.username, username) else DUMMY_PASSWORD_HASH
    try:
        verified = password_hasher.verify(candidate_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return None
    if not verified or admin is None or not hmac.compare_digest(admin.username, username):
        return None
    if password_hasher.check_needs_rehash(admin.password_hash):
        admin.password_hash = password_hasher.hash(password)
        session.commit()
    return admin


async def require_http_auth(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> AuthenticatedSession:
    authenticated = authenticate_token(session, request.cookies.get(settings.auth_cookie_name))
    if authenticated is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTHENTICATION_REQUIRED", "message": "Authentication is required"},
        )
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_trusted_origin(request.headers.get("origin"))
        csrf_token = request.headers.get(CSRF_HEADER)
        if not csrf_token or not hmac.compare_digest(digest(csrf_token), authenticated.session.csrf_digest):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "CSRF_INVALID", "message": "A valid CSRF token is required"},
            )
    request.state.authenticated = authenticated
    return authenticated


async def authenticate_websocket(websocket: WebSocket) -> str | None:
    origin = websocket.headers.get("origin")
    if not origin_is_trusted(origin):
        await websocket.close(code=4403, reason="Origin not allowed")
        return None
    try:
        with SessionLocal() as session:
            authenticated = authenticate_token(
                session, websocket.cookies.get(settings.auth_cookie_name)
            )
    except SQLAlchemyError:
        await websocket.close(code=1011, reason="Authentication service unavailable")
        return None
    if authenticated is None:
        await websocket.close(code=4401, reason="Authentication required")
        return None
    return authenticated.session.id


def websocket_session_active(session_id: str) -> bool:
    try:
        with SessionLocal() as session:
            saved = session.get(AuthSessionRecord, session_id)
            return bool(
                saved
                and saved.revoked_at is None
                and _aware(saved.expires_at) > utc_now()
            )
    except SQLAlchemyError:
        return False


async def wait_for_websocket_session_end(
    session_id: str, interval_seconds: float = 2.0
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        if not websocket_session_active(session_id):
            return
