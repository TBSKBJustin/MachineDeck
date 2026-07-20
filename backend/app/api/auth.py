from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database.models import AuditEventRecord
from app.database.session import get_session
from app.schemas.auth import AuthSessionResponse, AuthStatusResponse, Credentials
from app.security.auth import (
    AuthenticatedSession,
    administrator,
    authenticate_token,
    clear_login_failures,
    clear_session_cookie,
    create_administrator,
    create_auth_session,
    is_loopback,
    rate_limited,
    record_login_failure,
    rotate_csrf,
    require_http_auth,
    require_trusted_origin,
    set_session_cookie,
    utc_now,
    verify_credentials,
)


router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])
DatabaseSession = Annotated[Session, Depends(get_session)]
CurrentSession = Annotated[AuthenticatedSession, Depends(require_http_auth)]


def _session_response(
    username: str, expires_at: datetime, csrf_token: str
) -> AuthSessionResponse:
    return AuthSessionResponse(
        username=username,
        expires_at=expires_at,
        csrf_token=csrf_token,
    )


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status(request: Request, session: DatabaseSession) -> AuthStatusResponse:
    admin = administrator(session)
    authenticated = authenticate_token(
        session, request.cookies.get(settings.auth_cookie_name)
    )
    return AuthStatusResponse(
        setup_required=admin is None,
        authenticated=authenticated is not None,
    )


@router.post("/setup", response_model=AuthSessionResponse, status_code=status.HTTP_201_CREATED)
async def setup(
    credentials: Credentials,
    request: Request,
    response: Response,
    session: DatabaseSession,
) -> AuthSessionResponse:
    require_trusted_origin(request.headers.get("origin"))
    if not is_loopback(request.client.host if request.client else None):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "LOCAL_SETUP_REQUIRED",
                "message": "Initial administrator setup is allowed only from localhost",
            },
        )
    admin = create_administrator(session, credentials.username, credentials.password)
    saved, token, csrf_token = create_auth_session(session, admin)
    set_session_cookie(response, token, saved.expires_at)
    return _session_response(admin.username, saved.expires_at, csrf_token)


@router.post("/login", response_model=AuthSessionResponse)
async def login(
    credentials: Credentials,
    request: Request,
    response: Response,
    session: DatabaseSession,
) -> AuthSessionResponse:
    require_trusted_origin(request.headers.get("origin"))
    remote = request.client.host if request.client else "unknown"
    if rate_limited(session, remote):
        session.add(
            AuditEventRecord(
                id=str(uuid4()),
                actor="anonymous",
                action="auth.login",
                target_type="authentication",
                target_id="administrator",
                result="failure",
                details_json={"reason": "rate_limited"},
            )
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "LOGIN_RATE_LIMITED", "message": "Too many failed login attempts"},
            headers={"Retry-After": str(settings.login_window_minutes * 60)},
        )
    admin = verify_credentials(session, credentials.username, credentials.password)
    if admin is None:
        record_login_failure(session, remote, "invalid_credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "LOGIN_FAILED", "message": "Invalid username or password"},
        )
    clear_login_failures(session, remote)
    session.add(
        AuditEventRecord(
            id=str(uuid4()),
            actor=admin.username,
            action="auth.login",
            target_type="authentication",
            target_id=admin.id,
            result="success",
            details_json={},
        )
    )
    saved, token, csrf_token = create_auth_session(session, admin)
    set_session_cookie(response, token, saved.expires_at)
    return _session_response(admin.username, saved.expires_at, csrf_token)


@router.get("/session", response_model=AuthSessionResponse)
async def current_session(
    current: CurrentSession, session: DatabaseSession
) -> AuthSessionResponse:
    csrf_token = rotate_csrf(session, current.session)
    return _session_response(
        current.administrator.username, current.session.expires_at, csrf_token
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    current: CurrentSession,
    session: DatabaseSession,
) -> Response:
    current.session.revoked_at = utc_now()
    session.add(
        AuditEventRecord(
            id=str(uuid4()),
            actor=current.administrator.username,
            action="auth.logout",
            target_type="authentication",
            target_id=current.administrator.id,
            result="success",
            details_json={},
        )
    )
    session.commit()
    clear_session_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
