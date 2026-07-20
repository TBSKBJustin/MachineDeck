from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.adapters.runtime import AdapterResult, RuntimeState
from app.database.base import Base
from app.database.models import AuditEventRecord, ExecutionRecord
from app.orchestration.lifecycle_service import ApplicationLockRegistry, LifecycleService
from app.schemas.applications import ApplicationManifest
from app.schemas.lifecycle import ApplicationStatus
from app.services.applications import create_application


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
