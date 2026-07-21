from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.audit.service import AuditFilters, AuditQueryError, AuditService
from app.database.session import get_session
from app.schemas.audit import AuditCategory, AuditEventPage, AuditEventResponse


router = APIRouter(prefix="/api/v1/audit-events", tags=["audit"])
DatabaseSession = Annotated[Session, Depends(get_session)]


def _error(exc: AuditQueryError) -> HTTPException:
    return HTTPException(
        status_code=(
            status.HTTP_404_NOT_FOUND
            if exc.code == "AUDIT_EVENT_NOT_FOUND"
            else status.HTTP_400_BAD_REQUEST
        ),
        detail={"code": exc.code, "message": str(exc)},
    )


@router.get("", response_model=AuditEventPage)
async def list_audit_events(
    session: DatabaseSession,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    start: datetime | None = None,
    end: datetime | None = None,
    application_id: str | None = Query(default=None, min_length=1, max_length=64),
    actor: str | None = Query(default=None, min_length=1, max_length=100),
    action: str | None = Query(default=None, min_length=1, max_length=100),
    result: Literal["success", "failure"] | None = None,
    category: AuditCategory | None = None,
    execution_id: str | None = Query(default=None, min_length=1, max_length=36),
    keyword: str | None = Query(default=None, min_length=1, max_length=200),
) -> AuditEventPage:
    try:
        return AuditService(session).list(
            AuditFilters(
                limit=limit,
                cursor=cursor,
                start=start,
                end=end,
                application_id=application_id,
                actor=actor,
                action=action,
                result=result,
                category=category,
                execution_id=execution_id,
                keyword=keyword,
            )
        )
    except AuditQueryError as exc:
        raise _error(exc) from exc


@router.get("/{event_id}", response_model=AuditEventResponse)
async def get_audit_event(
    session: DatabaseSession,
    event_id: str = Path(min_length=1, max_length=36),
) -> AuditEventResponse:
    try:
        return AuditService(session).get(event_id)
    except AuditQueryError as exc:
        raise _error(exc) from exc
