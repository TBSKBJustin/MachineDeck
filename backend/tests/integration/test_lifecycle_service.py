from __future__ import annotations

from pathlib import Path
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.adapters.runtime import AdapterResult, RuntimeState
from app.database.base import Base
from app.database.models import AuditEventRecord, ExecutionRecord
from app.orchestration.lifecycle_service import (
    ApplicationLockRegistry,
    LifecycleError,
    LifecycleService,
)
from app.orchestration.port_discovery import PortDiscoveryError
from app.schemas.ports import PortConflict
from app.schemas.applications import ApplicationManifest
from app.schemas.lifecycle import ApplicationStatus
from app.services.applications import (
    ApplicationRunningError,
    create_application,
    delete_application,
    update_application,
)


class FakeAdapter:
    def __init__(self, fail_start: bool = False) -> None:
        self.state = ApplicationStatus.STOPPED
        self.fail_start = fail_start

    async def start(self) -> AdapterResult:
        if self.fail_start:
            return AdapterResult(False, "simulated failure", exit_code=1, error_code="TEST_FAILURE")
        self.state = ApplicationStatus.RUNNING
        return AdapterResult(True, "started", exit_code=0)

    async def stop(self) -> AdapterResult:
        self.state = ApplicationStatus.STOPPED
        return AdapterResult(True, "stopped", exit_code=0)

    async def restart(self) -> AdapterResult:
        self.state = ApplicationStatus.RUNNING
        return AdapterResult(True, "restarted", exit_code=0)

    async def status(self) -> RuntimeState:
        return RuntimeState(self.state, "fake-runtime")

    async def logs(self, lines: int) -> list[str]:
        return ["line one", "line two"][-lines:]


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as database_session:
        yield database_session
    engine.dispose()


def manifest(application_id: str) -> ApplicationManifest:
    project = Path(__file__).resolve().parents[1] / "fixtures" / "compose"
    return ApplicationManifest.model_validate(
        {
            "id": application_id,
            "name": application_id,
            "runtime": {
                "type": "compose",
                "working_dir": str(project),
                "compose_file": "compose.yaml",
            },
        }
    )


@pytest.mark.asyncio
async def test_lifecycle_state_and_executions_persist_across_service_instances(session: Session) -> None:
    create_application(session, manifest("stateful-app"))
    adapter = FakeAdapter()
    factory = lambda _: adapter
    lock_registry = ApplicationLockRegistry()

    started = await LifecycleService(session, factory, lock_registry).action("stateful-app", "start")
    assert started.succeeded
    assert started.status == ApplicationStatus.RUNNING

    # A new service object reconciles from the adapter, modeling backend restart behavior.
    status = await LifecycleService(session, factory, lock_registry).status("stateful-app")
    assert status.status == ApplicationStatus.RUNNING

    logs = await LifecycleService(session, factory, lock_registry).logs("stateful-app", 2)
    assert logs.source == "docker"
    assert logs.lines == ["line one", "line two"]

    stopped = await LifecycleService(session, factory, lock_registry).action("stateful-app", "stop")
    assert stopped.succeeded
    assert stopped.status == ApplicationStatus.STOPPED

    executions = session.scalars(select(ExecutionRecord).order_by(ExecutionRecord.started_at)).all()
    assert [(item.action, item.status) for item in executions] == [
        ("start", "SUCCEEDED"),
        ("stop", "SUCCEEDED"),
    ]
    lifecycle_audits = session.scalar(
        select(func.count())
        .select_from(AuditEventRecord)
        .where(AuditEventRecord.action.in_(["application.start", "application.stop"]))
    )
    assert lifecycle_audits == 2


@pytest.mark.asyncio
async def test_one_application_failure_does_not_change_another(session: Session) -> None:
    create_application(session, manifest("failing-app"))
    create_application(session, manifest("healthy-app"))
    adapters = {"failing-app": FakeAdapter(fail_start=True), "healthy-app": FakeAdapter()}
    factory = lambda saved_manifest: adapters[saved_manifest.id]
    locks = ApplicationLockRegistry()

    failed = await LifecycleService(session, factory, locks).action("failing-app", "start")
    healthy = await LifecycleService(session, factory, locks).action("healthy-app", "start")

    assert not failed.succeeded
    assert failed.status == ApplicationStatus.FAILED
    assert healthy.succeeded
    assert healthy.status == ApplicationStatus.RUNNING


@pytest.mark.asyncio
async def test_concurrent_start_serializes_delete_and_preserves_running_unit(session: Session) -> None:
    saved_manifest = manifest("locked-app")
    create_application(session, saved_manifest)
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowAdapter(FakeAdapter):
        async def start(self) -> AdapterResult:
            entered.set()
            await release.wait()
            return await super().start()

    lock_registry = ApplicationLockRegistry()
    lifecycle = LifecycleService(session, lambda _: SlowAdapter(), lock_registry)
    start_task = asyncio.create_task(lifecycle.action("locked-app", "start"))
    await entered.wait()

    async def attempt_delete() -> None:
        async with lock_registry.get("locked-app"):
            delete_application(session, "locked-app")

    delete_task = asyncio.create_task(attempt_delete())
    await asyncio.sleep(0)
    assert not delete_task.done()
    release.set()
    assert (await start_task).succeeded
    with pytest.raises(ApplicationRunningError):
        await delete_task


@pytest.mark.asyncio
async def test_running_application_configuration_cannot_be_updated(session: Session) -> None:
    original = manifest("immutable-running-app")
    create_application(session, original)
    adapter = FakeAdapter()
    service = LifecycleService(session, lambda _: adapter, ApplicationLockRegistry())
    assert (await service.action(original.id, "start")).succeeded
    changed = original.model_copy(update={"name": "Changed while running"})
    with pytest.raises(ApplicationRunningError):
        update_application(session, original.id, changed)


@pytest.mark.asyncio
async def test_start_port_conflict_is_audited_and_rejected_before_adapter(session: Session) -> None:
    saved = ApplicationManifest.model_validate(
        {
            **manifest("port-conflict-app").model_dump(mode="json"),
            "ports": [
                {
                    "id": "web",
                    "name": "Web",
                    "protocol": "http",
                    "host_port": 8188,
                }
            ],
        }
    )
    create_application(session, saved)
    adapter = FakeAdapter()
    with patch(
        "app.orchestration.lifecycle_service.PortService.conflicts",
        AsyncMock(
            return_value=[
                PortConflict(
                    protocol="tcp",
                    bind_address="0.0.0.0",
                    port=8188,
                    pid=99,
                    process_name="python",
                )
            ]
        ),
    ):
        with pytest.raises(LifecycleError) as raised:
            await LifecycleService(
                session,
                lambda _: adapter,
                ApplicationLockRegistry(),
                actor="admin",
                request_method="POST",
                request_path=f"/api/v1/applications/{saved.id}/start",
            ).action(saved.id, "start")
    assert raised.value.code == "PORT_CONFLICT"
    assert raised.value.details["conflicts"][0]["pid"] == 99
    assert adapter.state.value == "STOPPED"
    execution = session.scalar(
        select(ExecutionRecord).where(ExecutionRecord.application_id == saved.id)
    )
    assert execution.status == "FAILED"
    assert execution.error_code == "PORT_CONFLICT"
    audit = session.scalar(
        select(AuditEventRecord).where(
            AuditEventRecord.target_id == saved.id,
            AuditEventRecord.action == "application.start",
        )
    )
    assert audit.result == "failure"
    assert audit.actor == "admin"
    assert audit.details_json["execution_id"] == execution.id
    assert audit.details_json["conflicts"][0]["port"] == 8188
    assert audit.details_json["request"] == {
        "method": "POST",
        "path": f"/api/v1/applications/{saved.id}/start",
    }


@pytest.mark.asyncio
async def test_start_fails_closed_when_listener_scan_is_unavailable(session: Session) -> None:
    saved = ApplicationManifest.model_validate(
        {
            **manifest("port-scan-failure").model_dump(mode="json"),
            "ports": [{"id": "web", "name": "Web", "host_port": 8188}],
        }
    )
    create_application(session, saved)
    adapter = FakeAdapter()
    with patch(
        "app.orchestration.lifecycle_service.PortService.conflicts",
        AsyncMock(side_effect=PortDiscoveryError("permission denied")),
    ):
        with pytest.raises(LifecycleError) as raised:
            await LifecycleService(
                session, lambda _: adapter, ApplicationLockRegistry()
            ).action(saved.id, "start")
    assert raised.value.code == "PORT_DISCOVERY_UNAVAILABLE"
    assert adapter.state == ApplicationStatus.STOPPED
    execution = session.scalar(
        select(ExecutionRecord).where(ExecutionRecord.application_id == saved.id)
    )
    assert execution.error_code == "PORT_DISCOVERY_UNAVAILABLE"
