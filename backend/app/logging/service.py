from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Callable

import yaml
from sqlalchemy.orm import Session

from app.database.models import ApplicationRecord
from app.logging.adapters import RuntimeLogAdapter, log_adapter_for
from app.logging.models import LogEvent
from app.logging.redaction import LogRedactionService
from app.schemas.applications import ApplicationManifest, validate_manifest_paths


class ApplicationLogError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


LogAdapterFactory = Callable[[ApplicationManifest], RuntimeLogAdapter]
SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def parse_service_filter(value: str | None) -> set[str]:
    if not value:
        return set()
    services = value.split(",")
    if len(services) > 32 or any(not SERVICE_PATTERN.fullmatch(item) for item in services):
        raise ApplicationLogError("QUERY_INVALID", "Invalid services filter")
    return set(services)


class ApplicationLogService:
    def __init__(
        self, session: Session, adapter_factory: LogAdapterFactory = log_adapter_for
    ) -> None:
        self.session = session
        self.adapter_factory = adapter_factory

    def _context(
        self, application_id: str
    ) -> tuple[RuntimeLogAdapter, LogRedactionService]:
        record = self.session.get(ApplicationRecord, application_id)
        if record is None:
            raise ApplicationLogError("APP_NOT_FOUND", f"Application not found: {application_id}")
        manifest = ApplicationManifest.model_validate(yaml.safe_load(record.config_yaml))
        validation = validate_manifest_paths(manifest)
        if not validation.valid:
            message = "; ".join(issue.message for issue in validation.errors)
            raise ApplicationLogError("CONFIG_INVALID", message)
        return self.adapter_factory(manifest), LogRedactionService(manifest)

    @staticmethod
    def _prepare(
        event: LogEvent,
        redaction: LogRedactionService,
        services: set[str],
    ) -> LogEvent | None:
        if services and event.service not in services:
            return None
        return event.model_copy(update={"message": redaction.redact(event.message)})

    async def history(
        self,
        application_id: str,
        *,
        limit: int,
        since: datetime | None,
        cursor: str | None,
        services: set[str],
    ) -> tuple[list[LogEvent], RuntimeLogAdapter, LogRedactionService]:
        adapter, redaction = self._context(application_id)
        events = await adapter.history(limit=limit, since=since, cursor=cursor)
        prepared = [self._prepare(event, redaction, services) for event in events]
        return [event for event in prepared if event is not None], adapter, redaction

    async def follow(
        self,
        adapter: RuntimeLogAdapter,
        redaction: LogRedactionService,
        *,
        cursor: str | None,
        services: set[str],
    ) -> AsyncIterator[LogEvent]:
        async for event in adapter.follow(cursor=cursor):
            prepared = self._prepare(event, redaction, services)
            if prepared is not None:
                yield prepared
