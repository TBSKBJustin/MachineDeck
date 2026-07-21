from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.dashboard.collectors import (
    ApplicationSummaryCollector,
    DashboardCollector,
    GpuMetricsCollector,
    HostMetricsCollector,
)
from app.dashboard.ownership import GpuProcessOwnershipService
from app.database.base import Base
from app.database.models import ApplicationInstanceRecord, ApplicationRecord


class FakePsutil:
    def __init__(self, disk_error: Exception | None = None) -> None:
        self.disk_error = disk_error
        self.cpu_call = None

    def cpu_percent(self, **kwargs: object) -> list[float]:
        self.cpu_call = kwargs
        return [10.0, 30.0]

    @staticmethod
    def virtual_memory() -> SimpleNamespace:
        return SimpleNamespace(total=2**50, used=2**49, available=2**49, percent=50.0)

    @staticmethod
    def swap_memory() -> SimpleNamespace:
        return SimpleNamespace(total=0, used=0, percent=0.0)

    @staticmethod
    def getloadavg() -> tuple[float, float, float]:
        return (1.0, 2.0, 3.0)

    @staticmethod
    def boot_time() -> float:
        return 1.0

    def disk_usage(self, _: str) -> SimpleNamespace:
        if self.disk_error:
            raise self.disk_error
        return SimpleNamespace(total=2**50, used=2**49, free=2**49, percent=50.0)

    @staticmethod
    def disk_partitions(**_: object) -> list[SimpleNamespace]:
        return [SimpleNamespace(mountpoint="/", fstype="ext4")]


def test_host_collection_primes_cpu_and_handles_no_swap_and_large_values(tmp_path: Path) -> None:
    fake = FakePsutil()
    metrics, states = HostMetricsCollector((tmp_path,), fake).collect()
    assert fake.cpu_call == {"interval": 0.1, "percpu": True}
    assert metrics.cpu_percent == 20.0
    assert metrics.swap_total_bytes == 0
    assert metrics.memory_total_bytes == 2**50
    assert metrics.disks[0].total_bytes == 2**50
    assert states["host"].available


def test_missing_and_unmounted_disks_degrade_without_breaking_host(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    metrics, states = HostMetricsCollector((missing,), FakePsutil()).collect()
    assert not metrics.disks[0].available
    assert states["disks"].error_code == "DISK_METRICS_UNAVAILABLE"

    metrics, states = HostMetricsCollector((tmp_path,), FakePsutil(OSError("unmounted"))).collect()
    assert not metrics.disks[0].available
    assert metrics.cpu_percent == 20.0
    assert not states["disks"].available


class FakeNvml:
    NVML_TEMPERATURE_GPU = 0

    def __init__(self, count: int = 2) -> None:
        self.count = count
        self.initialized = 0
        self.shutdown = 0

    def nvmlInit(self) -> None:
        self.initialized += 1

    def nvmlShutdown(self) -> None:
        self.shutdown += 1

    def nvmlDeviceGetCount(self) -> int:
        return self.count

    @staticmethod
    def nvmlDeviceGetHandleByIndex(index: int) -> int:
        return index

    @staticmethod
    def nvmlDeviceGetMemoryInfo(handle: int) -> SimpleNamespace:
        if handle == 1:
            raise RuntimeError("GPU is lost")
        return SimpleNamespace(total=24 * 2**30, used=12 * 2**30, free=12 * 2**30)

    @staticmethod
    def nvmlDeviceGetUtilizationRates(_: int) -> SimpleNamespace:
        return SimpleNamespace(gpu=75)

    @staticmethod
    def nvmlDeviceGetUUID(handle: int) -> bytes:
        return f"GPU-{handle}".encode()

    @staticmethod
    def nvmlDeviceGetName(_: int) -> bytes:
        return b"RTX 3090"

    @staticmethod
    def nvmlDeviceGetTemperature(*_: object) -> int:
        raise RuntimeError("not supported")

    @staticmethod
    def nvmlDeviceGetPowerUsage(_: int) -> int:
        return 281000

    @staticmethod
    def nvmlDeviceGetEnforcedPowerLimit(_: int) -> int:
        return 350000

    @staticmethod
    def nvmlDeviceGetFanSpeed(*_: object) -> int:
        raise RuntimeError("not supported")

    @staticmethod
    def nvmlDeviceGetComputeRunningProcesses(_: int) -> list:
        return []

    @staticmethod
    def nvmlDeviceGetGraphicsRunningProcesses(_: int) -> list:
        return []


def test_nvml_missing_and_no_gpu_are_valid_degraded_states() -> None:
    missing = GpuMetricsCollector(module_loader=lambda _: (_ for _ in ()).throw(ImportError()))
    gpus, state = missing.collect()
    assert gpus == []
    assert state.error_code == "NVML_NOT_AVAILABLE"

    module = FakeNvml(count=0)
    gpus, state = GpuMetricsCollector(module_loader=lambda _: module).collect()
    assert gpus == []
    assert state.available


def test_one_gpu_failure_and_optional_sensor_failures_do_not_hide_other_gpu() -> None:
    module = FakeNvml()
    collector = GpuMetricsCollector(module_loader=lambda _: module)
    gpus, state = collector.collect()
    assert len(gpus) == 2
    assert gpus[0].available and not gpus[1].available
    assert gpus[0].temperature_celsius is None
    assert gpus[0].fan_percent is None
    assert gpus[0].power_usage_watts == 281
    assert state.error_code == "GPU_METRICS_PARTIAL"
    collector.close()
    assert module.shutdown == 1


class RecoveringNvml(FakeNvml):
    def __init__(self) -> None:
        super().__init__(count=0)
        self.count_calls = 0

    def nvmlDeviceGetCount(self) -> int:
        self.count_calls += 1
        if self.count_calls == 1:
            raise RuntimeError("driver reset")
        return 0


def test_nvml_runtime_failure_is_retried_on_next_collection() -> None:
    module = RecoveringNvml()
    collector = GpuMetricsCollector(module_loader=lambda _: module)
    _, failed = collector.collect()
    gpus, recovered = collector.collect()
    assert not failed.available
    assert recovered.available and gpus == []
    assert module.initialized == 2


def test_gpu_process_enrichment_keeps_managed_ownership_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.dashboard.ownership.psutil.Process", lambda _: SimpleNamespace(name=lambda: "python"))
    processes = GpuProcessOwnershipService().enrich(
        [(123, 2**30), (456, None)], managed_pid_map={123: "managed-app"}
    )
    assert processes[0].managed and processes[0].application_id == "managed-app"
    assert not processes[1].managed and processes[1].application_id is None


def test_application_summary_uses_latest_persisted_state() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                ApplicationRecord(
                    id="running-app",
                    name="Running",
                    description="",
                    runtime_type="compose",
                    config_yaml="{}",
                    enabled=True,
                ),
                ApplicationRecord(
                    id="disabled-app",
                    name="Disabled",
                    description="",
                    runtime_type="compose",
                    config_yaml="{}",
                    enabled=False,
                ),
                ApplicationRecord(
                    id="new-app",
                    name="New",
                    description="",
                    runtime_type="compose",
                    config_yaml="{}",
                    enabled=True,
                ),
            ]
        )
        session.commit()
        session.add(
            ApplicationInstanceRecord(
                id=str(uuid4()), application_id="running-app", status="RUNNING"
            )
        )
        session.commit()
    summary, state = ApplicationSummaryCollector(factory).collect()
    assert state.available
    assert summary.total == 3
    assert summary.running == 1
    assert summary.stopped == 1
    assert summary.disabled == 1
    engine.dispose()


class RaisingCollector:
    def collect(self) -> object:
        raise RuntimeError("internal path must not leak")


class EmptyGpu:
    def collect(self) -> tuple[list, object]:
        from app.schemas.dashboard import CollectorState

        return [], CollectorState(available=True)

    def close(self) -> None:
        return


class EmptyApplications:
    def collect(self) -> tuple[object, object]:
        from app.schemas.dashboard import ApplicationSummary, CollectorState

        return ApplicationSummary(), CollectorState(available=True)


@pytest.mark.asyncio
async def test_collector_exception_does_not_break_other_collectors() -> None:
    snapshot = await DashboardCollector(
        host=RaisingCollector(),
        gpu=EmptyGpu(),
        applications=EmptyApplications(),
        offload=False,
    ).collect()
    assert not snapshot.collectors["host"].available
    assert snapshot.collectors["nvml"].available
    assert "internal path" not in (snapshot.collectors["host"].message or "")
    assert snapshot.collected_at <= datetime.now(timezone.utc)
