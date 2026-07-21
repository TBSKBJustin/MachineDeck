from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.audit.service import AuditFilters, AuditQueryError, AuditService
from app.database.base import Base
from app.database.models import ApplicationRecord, AuditEventRecord, ExecutionRecord
from app.schemas.audit import AuditCategory


def event_id(number: int) -> str:
    return f"00000000-0000-0000-0000-{number:012d}"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as database_session:
        yield database_session
    engine.dispose()


@pytest.fixture
def seeded(session: Session) -> datetime:
    timestamp = datetime(2026, 7, 20, 21, 30, tzinfo=timezone.utc)
    session.add(
        ApplicationRecord(
            id="comfyui",
            name="ComfyUI",
            description="",
            runtime_type="process",
            config_yaml="{}",
            enabled=True,
        )
    )
    session.add(
        ExecutionRecord(
            id=event_id(90),
            application_id="comfyui",
            action="start",
            status="FAILED",
            requested_by="admin",
            started_at=timestamp,
            finished_at=timestamp,
            error_code="PORT_CONFLICT",
        )
    )
    session.commit()
    session.add_all(
        [
            AuditEventRecord(
                id=event_id(1),
                actor="admin",
                action="application.create",
                target_type="application",
                target_id="comfyui",
                result="success",
                details_json={},
                created_at=timestamp,
            ),
            AuditEventRecord(
                id=event_id(2),
                actor="phase1-api",
                action="application.start",
                target_type="application",
                target_id="comfyui",
                result="failure",
                details_json={
                    "execution_id": event_id(90),
                    "error_code": "PORT_CONFLICT",
                    "password": "never-return-this",
                    "request": {
                        "method": "POST",
                        "path": "/api/v1/applications/comfyui/start?token=secret",
                    },
                },
                created_at=timestamp,
            ),
            AuditEventRecord(
                id=event_id(3),
                actor="anonymous",
                action="auth.login",
                target_type="authentication",
                target_id="administrator",
                result="failure",
                details_json={
                    "reason": "invalid_credentials",
                    "password": "submitted-password",
                },
                created_at=timestamp,
            ),
            AuditEventRecord(
                id=event_id(4),
                actor="legacy-agent",
                action="legacy.unknown",
                target_type="legacy-target",
                target_id="old-item",
                result="success",
                details_json={"message": "old event remains readable"},
                created_at=timestamp - timedelta(seconds=1),
            ),
            AuditEventRecord(
                id=event_id(5),
                actor="phase1-api",
                action="application.delete",
                target_type="application",
                target_id="deleted-app",
                result="success",
                details_json={"target_name": "Deleted application"},
                created_at=timestamp - timedelta(seconds=2),
            ),
        ]
    )
    session.commit()
    return timestamp


def test_cursor_pagination_is_stable_for_identical_timestamps(
    session: Session, seeded: datetime
) -> None:
    service = AuditService(session)
    first = service.list(AuditFilters(limit=2))
    second = service.list(AuditFilters(limit=2, cursor=first.next_cursor))
    third = service.list(AuditFilters(limit=2, cursor=second.next_cursor))
    ids = [event.id for page in (first, second, third) for event in page.events]
    assert ids == [event_id(3), event_id(2), event_id(1), event_id(4), event_id(5)]
    assert len(ids) == len(set(ids))
    assert first.has_more and second.has_more and not third.has_more


def test_filters_execution_link_and_deleted_application_history(
    session: Session, seeded: datetime
) -> None:
    service = AuditService(session)
    lifecycle = service.list(
        AuditFilters(
            category=AuditCategory.APPLICATION_LIFECYCLE,
            application_id="comfyui",
            result="failure",
            action="start",
            execution_id=event_id(90),
        )
    )
    assert len(lifecycle.events) == 1
    item = lifecycle.events[0]
    assert item.execution.status == "FAILED"
    assert item.execution.error_code == "PORT_CONFLICT"
    assert item.target.name == "ComfyUI"
    assert item.request.path == "/api/v1/applications/comfyui/start"
    assert item.details["password"] == "[REDACTED]"

    deleted = service.list(AuditFilters(application_id="deleted-app"))
    assert deleted.events[0].target.id == "deleted-app"
    assert deleted.events[0].target.name == "Deleted application"
    assert "target_name" not in deleted.events[0].details


def test_auth_normalization_time_keyword_and_legacy_events(
    session: Session, seeded: datetime
) -> None:
    service = AuditService(session)
    failed = service.list(
        AuditFilters(
            category=AuditCategory.AUTHENTICATION,
            action="LOGIN_FAILED",
            start=seeded - timedelta(seconds=1),
            end=seeded + timedelta(seconds=1),
        )
    )
    assert [event.action for event in failed.events] == ["LOGIN_FAILED"]
    assert "submitted-password" not in str(failed.model_dump())

    keyword = service.list(AuditFilters(keyword="legacy-agent"))
    assert keyword.events[0].category == AuditCategory.OTHER
    assert keyword.events[0].details["message"] == "old event remains readable"


def test_invalid_cursor_and_time_range_are_rejected(session: Session, seeded: datetime) -> None:
    service = AuditService(session)
    with pytest.raises(AuditQueryError, match="cursor"):
        service.list(AuditFilters(cursor="not-a-valid-cursor"))
    with pytest.raises(AuditQueryError, match="before"):
        service.list(AuditFilters(start=seeded, end=seeded - timedelta(seconds=1)))
