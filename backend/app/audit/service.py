from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.database.models import ApplicationRecord, AuditEventRecord, ExecutionRecord
from app.schemas.audit import (
    AuditActor,
    AuditCategory,
    AuditEventPage,
    AuditEventResponse,
    AuditExecutionLink,
    AuditRequest,
    AuditTarget,
)

from .redaction import sanitize_audit_details


class AuditQueryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AuditFilters:
    limit: int = 50
    cursor: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    application_id: str | None = None
    actor: str | None = None
    action: str | None = None
    result: str | None = None
    category: AuditCategory | None = None
    execution_id: str | None = None
    keyword: str | None = None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _encode_cursor(record: AuditEventRecord) -> str:
    payload = json.dumps(
        {"timestamp": _aware(record.created_at).isoformat(), "id": record.id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(decoded)
        timestamp = datetime.fromisoformat(str(payload["timestamp"]))
        event_id = str(payload["id"])
        if timestamp.tzinfo is None or not 1 <= len(event_id) <= 36:
            raise ValueError
        return timestamp, event_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise AuditQueryError("CURSOR_INVALID", "Audit cursor is invalid") from exc


def category_for(action: str, target_type: str) -> AuditCategory:
    if action.startswith("auth.") or target_type == "authentication":
        return AuditCategory.AUTHENTICATION
    if action in {"application.create", "application.update", "application.delete"}:
        return AuditCategory.APPLICATION_REGISTRY
    if action in {"application.start", "application.stop", "application.restart"}:
        return AuditCategory.APPLICATION_LIFECYCLE
    if action.startswith("system.") or target_type == "system":
        return AuditCategory.SYSTEM
    return AuditCategory.OTHER


def normalized_action(record: AuditEventRecord, details: dict[str, Any]) -> str:
    if record.action == "auth.setup":
        return "SETUP_COMPLETED"
    if record.action == "auth.login":
        if record.result == "success":
            return "LOGIN_SUCCEEDED"
        if details.get("reason") == "rate_limited":
            return "RATE_LIMITED"
        return "LOGIN_FAILED"
    if record.action == "auth.logout":
        return "LOGOUT"
    return record.action.rsplit(".", 1)[-1]


def actor_for(value: str) -> AuditActor:
    if value == "anonymous":
        actor_type = "anonymous"
    elif value in {"phase1-api", "system", "scheduler"}:
        actor_type = "system"
    elif value:
        actor_type = "administrator"
    else:
        actor_type = "unknown"
    return AuditActor(type=actor_type, id=value or "unknown")


class AuditService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self, filters: AuditFilters) -> AuditEventPage:
        if filters.start and filters.start.tzinfo is None:
            raise AuditQueryError("TIME_INVALID", "start must include a timezone")
        if filters.end and filters.end.tzinfo is None:
            raise AuditQueryError("TIME_INVALID", "end must include a timezone")
        if filters.start and filters.end and filters.start > filters.end:
            raise AuditQueryError("TIME_INVALID", "start must be before end")

        statement = select(AuditEventRecord)
        conditions = []
        if filters.start:
            conditions.append(AuditEventRecord.created_at >= filters.start)
        if filters.end:
            conditions.append(AuditEventRecord.created_at <= filters.end)
        if filters.application_id:
            conditions.extend(
                [
                    AuditEventRecord.target_type == "application",
                    AuditEventRecord.target_id == filters.application_id,
                ]
            )
        if filters.actor:
            conditions.append(AuditEventRecord.actor == filters.actor)
        if filters.action:
            normalized = filters.action.upper()
            if normalized == "LOGIN_SUCCEEDED":
                conditions.extend(
                    [AuditEventRecord.action == "auth.login", AuditEventRecord.result == "success"]
                )
            elif normalized in {"LOGIN_FAILED", "RATE_LIMITED"}:
                conditions.extend(
                    [AuditEventRecord.action == "auth.login", AuditEventRecord.result == "failure"]
                )
                reason = "rate_limited" if normalized == "RATE_LIMITED" else "invalid_credentials"
                conditions.append(
                    AuditEventRecord.details_json["reason"].as_string() == reason
                )
            elif normalized == "SETUP_COMPLETED":
                conditions.append(AuditEventRecord.action == "auth.setup")
            elif normalized == "LOGOUT":
                conditions.append(AuditEventRecord.action == "auth.logout")
            else:
                raw_action = (
                    filters.action
                    if "." in filters.action
                    else f"application.{filters.action.lower()}"
                )
                conditions.append(AuditEventRecord.action == raw_action)
        if filters.result:
            conditions.append(AuditEventRecord.result == filters.result)
        if filters.category:
            conditions.append(self._category_condition(filters.category))
        if filters.execution_id:
            conditions.append(
                AuditEventRecord.details_json["execution_id"].as_string()
                == filters.execution_id
            )
        if filters.keyword:
            escaped = (
                filters.keyword.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            conditions.append(
                or_(
                    AuditEventRecord.actor.ilike(pattern, escape="\\"),
                    AuditEventRecord.action.ilike(pattern, escape="\\"),
                    AuditEventRecord.target_id.ilike(pattern, escape="\\"),
                    AuditEventRecord.result.ilike(pattern, escape="\\"),
                )
            )
        if filters.cursor:
            timestamp, event_id = _decode_cursor(filters.cursor)
            conditions.append(
                or_(
                    AuditEventRecord.created_at < timestamp,
                    and_(
                        AuditEventRecord.created_at == timestamp,
                        AuditEventRecord.id < event_id,
                    ),
                )
            )
        if conditions:
            statement = statement.where(*conditions)
        records = self.session.scalars(
            statement.order_by(
                AuditEventRecord.created_at.desc(), AuditEventRecord.id.desc()
            ).limit(filters.limit + 1)
        ).all()
        has_more = len(records) > filters.limit
        page_records = records[: filters.limit]
        events = self._map_many(page_records)
        return AuditEventPage(
            events=events,
            has_more=has_more,
            next_cursor=_encode_cursor(page_records[-1]) if has_more and page_records else None,
        )

    def get(self, event_id: str) -> AuditEventResponse:
        record = self.session.get(AuditEventRecord, event_id)
        if record is None:
            raise AuditQueryError("AUDIT_EVENT_NOT_FOUND", "Audit event not found")
        return self._map_many([record])[0]

    @staticmethod
    def _category_condition(category: AuditCategory) -> Any:
        if category == AuditCategory.AUTHENTICATION:
            return or_(
                AuditEventRecord.action.like("auth.%"),
                AuditEventRecord.target_type == "authentication",
            )
        if category == AuditCategory.APPLICATION_REGISTRY:
            return AuditEventRecord.action.in_(
                ["application.create", "application.update", "application.delete"]
            )
        if category == AuditCategory.APPLICATION_LIFECYCLE:
            return AuditEventRecord.action.in_(
                ["application.start", "application.stop", "application.restart"]
            )
        if category == AuditCategory.SYSTEM:
            return or_(
                AuditEventRecord.action.like("system.%"),
                AuditEventRecord.target_type == "system",
            )
        known = or_(
            AuditEventRecord.action.like("auth.%"),
            AuditEventRecord.action.in_(
                [
                    "application.create",
                    "application.update",
                    "application.delete",
                    "application.start",
                    "application.stop",
                    "application.restart",
                ]
            ),
            AuditEventRecord.action.like("system.%"),
        )
        return ~known

    def _map_many(self, records: list[AuditEventRecord]) -> list[AuditEventResponse]:
        application_ids = {
            record.target_id for record in records if record.target_type == "application"
        }
        applications = {
            record.id: record.name
            for record in self.session.scalars(
                select(ApplicationRecord).where(ApplicationRecord.id.in_(application_ids))
            ).all()
        } if application_ids else {}
        raw_details = {record.id: sanitize_audit_details(record.details_json) for record in records}
        execution_ids = {
            str(details.get("execution_id"))
            for details in raw_details.values()
            if details.get("execution_id")
        }
        executions = {
            record.id: record
            for record in self.session.scalars(
                select(ExecutionRecord).where(ExecutionRecord.id.in_(execution_ids))
            ).all()
        } if execution_ids else {}
        output = []
        for record in records:
            details = raw_details[record.id]
            execution_id = details.get("execution_id")
            execution = executions.get(str(execution_id)) if execution_id else None
            target_name = details.pop("target_name", None)
            details.pop("request", None)
            request_data = (
                record.details_json.get("request")
                if isinstance(record.details_json, dict)
                else None
            )
            request = None
            if isinstance(request_data, dict):
                method = request_data.get("method")
                path = request_data.get("path")
                safe_method = (
                    method.upper()
                    if isinstance(method, str)
                    and method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                    else None
                )
                safe_path = (
                    path.split("?", 1)[0][:500]
                    if isinstance(path, str) and path.startswith("/api/v1/")
                    else None
                )
                request = AuditRequest(
                    method=safe_method, path=safe_path
                )
            output.append(
                AuditEventResponse(
                    id=record.id,
                    timestamp=_aware(record.created_at),
                    actor=actor_for(record.actor),
                    category=category_for(record.action, record.target_type),
                    action=normalized_action(record, details),
                    raw_action=record.action,
                    result=record.result,
                    target=AuditTarget(
                        type=record.target_type,
                        id=record.target_id,
                        name=applications.get(record.target_id) or target_name,
                    ),
                    execution_id=str(execution_id) if execution_id else None,
                    execution=AuditExecutionLink(
                        id=execution.id,
                        action=execution.action,
                        status=execution.status,
                        started_at=execution.started_at,
                        finished_at=execution.finished_at,
                        error_code=execution.error_code,
                    ) if execution else None,
                    request=request,
                    details=details,
                )
            )
        return output
