from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.runtime import RuntimeAdapter, RuntimeState, adapter_for
from app.database.models import (
    ApplicationInstanceRecord,
    ApplicationRecord,
    AuditEventRecord,
    ExecutionRecord,
)
from app.schemas.applications import ApplicationManifest, validate_manifest_paths
from app.schemas.lifecycle import (
    ApplicationStateResponse,
    ApplicationStatus,
    ExecutionStatus,
    LifecycleActionResponse,
    LogResponse,
)
from app.orchestration.port_discovery import PortDiscoveryError

from .state_machine import InvalidStateTransitionError, require_transition
from .ports import PortService


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LifecycleError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


AdapterFactory = Callable[[ApplicationManifest], RuntimeAdapter]


class ApplicationLockRegistry:
    def __init__(self) -> None:
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def get(self, application_id: str) -> asyncio.Lock:
        return self._locks[application_id]


locks = ApplicationLockRegistry()


class LifecycleService:
    def __init__(
        self,
        session: Session,
        adapter_factory: AdapterFactory = adapter_for,
        lock_registry: ApplicationLockRegistry = locks,
    ) -> None:
        self.session = session
        self.adapter_factory = adapter_factory
        self.lock_registry = lock_registry

    def _application(self, application_id: str) -> tuple[ApplicationRecord, ApplicationManifest]:
        record = self.session.get(ApplicationRecord, application_id)
        if record is None:
            raise LifecycleError("APP_NOT_FOUND", f"Application not found: {application_id}")
        manifest = ApplicationManifest.model_validate(yaml.safe_load(record.config_yaml))
        validation = validate_manifest_paths(manifest)
        if not validation.valid:
            messages = "; ".join(issue.message for issue in validation.errors)
            raise LifecycleError("CONFIG_INVALID", messages)
        return record, manifest

    def _state_record(self, application_id: str) -> ApplicationInstanceRecord:
        state = self.session.scalar(
            select(ApplicationInstanceRecord)
            .where(ApplicationInstanceRecord.application_id == application_id)
            .order_by(ApplicationInstanceRecord.created_at.desc())
            .limit(1)
        )
        if state is None:
            state = ApplicationInstanceRecord(
                id=str(uuid4()),
                application_id=application_id,
                status=ApplicationStatus.STOPPED.value,
                metadata_json={},
            )
            self.session.add(state)
            self.session.flush()
        return state

    @staticmethod
    def _state_response(state: ApplicationInstanceRecord) -> ApplicationStateResponse:
        return ApplicationStateResponse(
            application_id=state.application_id,
            instance_id=state.id,
            status=ApplicationStatus(state.status),
            runtime_identifier=state.runtime_identifier,
            started_at=state.started_at,
            stopped_at=state.stopped_at,
            exit_code=state.exit_code,
            error_message=state.error_message,
            updated_at=state.updated_at,
        )

    def _transition(
        self,
        state: ApplicationInstanceRecord,
        target: ApplicationStatus,
        *,
        error_message: str | None = None,
        enforce: bool = True,
    ) -> None:
        current = ApplicationStatus(state.status)
        if enforce:
            require_transition(current, target)
        state.status = target.value
        state.error_message = error_message
        if target == ApplicationStatus.RUNNING:
            state.started_at = state.started_at or utc_now()
            state.stopped_at = None
        elif target == ApplicationStatus.STOPPED:
            state.stopped_at = utc_now()
        self.session.commit()

    def _apply_runtime_state(
        self, state: ApplicationInstanceRecord, runtime_state: RuntimeState
    ) -> ApplicationStateResponse:
        state.status = runtime_state.status.value
        state.runtime_identifier = runtime_state.runtime_identifier
        state.metadata_json = runtime_state.metadata
        state.error_message = runtime_state.error_message
        if runtime_state.status == ApplicationStatus.RUNNING:
            state.started_at = state.started_at or utc_now()
            state.stopped_at = None
        elif runtime_state.status == ApplicationStatus.STOPPED:
            state.stopped_at = utc_now()
        self.session.commit()
        self.session.refresh(state)
        return self._state_response(state)

    def _new_execution(self, application_id: str, action: str) -> ExecutionRecord:
        execution = ExecutionRecord(
            id=str(uuid4()),
            application_id=application_id,
            action=action,
            status=ExecutionStatus.RUNNING.value,
            requested_by="phase1-api",
            started_at=utc_now(),
        )
        self.session.add(execution)
        self.session.commit()
        return execution

    def _finish_execution(
        self,
        execution: ExecutionRecord,
        succeeded: bool,
        *,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        execution.status = (
            ExecutionStatus.SUCCEEDED.value if succeeded else ExecutionStatus.FAILED.value
        )
        execution.finished_at = utc_now()
        execution.exit_code = exit_code
        execution.error_code = error_code
        execution.error_message = error_message
        self.session.add(
            AuditEventRecord(
                id=str(uuid4()),
                actor="phase1-api",
                action=f"application.{execution.action}",
                target_type="application",
                target_id=execution.application_id,
                result="success" if succeeded else "failure",
                details_json={
                    "execution_id": execution.id,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            )
        )
        self.session.commit()

    async def status(self, application_id: str) -> ApplicationStateResponse:
        record, manifest = self._application(application_id)
        state = self._state_record(application_id)
        if not record.enabled:
            state.status = ApplicationStatus.DISABLED.value
            self.session.commit()
            return self._state_response(state)
        async with self.lock_registry.get(application_id):
            runtime_state = await self.adapter_factory(manifest).status()
            return self._apply_runtime_state(state, runtime_state)

    async def logs(self, application_id: str, lines: int) -> LogResponse:
        if not 1 <= lines <= 5000:
            raise LifecycleError("CONFIG_INVALID", "lines must be between 1 and 5000")
        _, manifest = self._application(application_id)
        async with self.lock_registry.get(application_id):
            adapter = self.adapter_factory(manifest)
            try:
                output = await adapter.logs(lines)
            except RuntimeError as exc:
                raise LifecycleError("LOG_READ_FAILED", str(exc)) from exc
        source = "journal" if manifest.runtime.type == "process" else "docker"
        return LogResponse(application_id=application_id, source=source, lines=output)

    async def action(self, application_id: str, action: str) -> LifecycleActionResponse:
        if action not in {"start", "stop", "restart"}:
            raise LifecycleError("ACTION_INVALID", f"Unsupported lifecycle action: {action}")
        async with self.lock_registry.get(application_id):
            record, manifest = self._application(application_id)
            if not record.enabled:
                raise LifecycleError("APP_DISABLED", f"Application is disabled: {application_id}")
            state = self._state_record(application_id)
            current = ApplicationStatus(state.status)
            if action == "start" and current in {
                ApplicationStatus.RUNNING,
                ApplicationStatus.STARTING,
                ApplicationStatus.CHECKING,
            }:
                raise LifecycleError("APP_ALREADY_RUNNING", f"Application is already running: {application_id}")
            if action in {"stop", "restart"} and current in {
                ApplicationStatus.STOPPED,
                ApplicationStatus.DISABLED,
            }:
                raise LifecycleError("APP_NOT_RUNNING", f"Application is not running: {application_id}")

            execution = self._new_execution(application_id, action)

            if action in {"start", "restart"}:
                try:
                    conflicts = await PortService(self.session).conflicts(manifest)
                except PortDiscoveryError as exc:
                    self._finish_execution(
                        execution,
                        False,
                        error_code="PORT_DISCOVERY_UNAVAILABLE",
                        error_message=str(exc),
                    )
                    raise LifecycleError(
                        "PORT_DISCOVERY_UNAVAILABLE",
                        "Cannot safely validate declared ports before startup",
                    ) from exc
                if conflicts:
                    self._finish_execution(
                        execution,
                        False,
                        error_code="PORT_CONFLICT",
                        error_message="One or more declared ports are already in use",
                    )
                    raise LifecycleError(
                        "PORT_CONFLICT",
                        f"One or more declared ports are already in use for {application_id}",
                        details={
                            "conflicts": [conflict.model_dump(mode="json") for conflict in conflicts]
                        },
                    )

            adapter = self.adapter_factory(manifest)
            try:
                if action == "start":
                    self._transition(state, ApplicationStatus.CHECKING)
                    self._transition(state, ApplicationStatus.STARTING)
                elif action == "stop":
                    self._transition(state, ApplicationStatus.STOPPING)
                else:
                    self._transition(state, ApplicationStatus.STARTING)
            except InvalidStateTransitionError as exc:
                self._finish_execution(
                    execution, False, error_code="STATE_TRANSITION_INVALID", error_message=str(exc)
                )
                raise LifecycleError("STATE_TRANSITION_INVALID", str(exc)) from exc

            result = await getattr(adapter, action)()
            if not result.succeeded:
                self._transition(
                    state,
                    ApplicationStatus.FAILED,
                    error_message=result.message,
                    enforce=False,
                )
                self._finish_execution(
                    execution,
                    False,
                    exit_code=result.exit_code,
                    error_code=result.error_code,
                    error_message=result.message,
                )
                return LifecycleActionResponse(
                    application_id=application_id,
                    execution_id=execution.id,
                    action=action,
                    status=ApplicationStatus.FAILED,
                    succeeded=False,
                    error_code=result.error_code,
                    message=result.message,
                )

            runtime_state = await adapter.status()
            state_response = self._apply_runtime_state(state, runtime_state)
            expected = (
                {
                    ApplicationStatus.STARTING,
                    ApplicationStatus.RUNNING,
                    ApplicationStatus.UNHEALTHY,
                }
                if action in {"start", "restart"}
                else {ApplicationStatus.STOPPED}
            )
            succeeded = state_response.status in expected
            error_code = None if succeeded else "STATE_CONFIRMATION_FAILED"
            message = result.message or runtime_state.error_message or ""
            self._finish_execution(
                execution,
                succeeded,
                exit_code=result.exit_code,
                error_code=error_code,
                error_message=None if succeeded else message,
            )
            return LifecycleActionResponse(
                application_id=application_id,
                execution_id=execution.id,
                action=action,
                status=state_response.status,
                succeeded=succeeded,
                error_code=error_code,
                message=message,
            )
