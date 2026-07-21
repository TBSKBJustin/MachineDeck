from __future__ import annotations

import asyncio
import importlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import psutil
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database.models import ApplicationInstanceRecord, ApplicationRecord
from app.database.session import SessionLocal
from app.schemas.dashboard import (
    ApplicationSummary,
    CollectorState,
    DashboardSnapshot,
    DiskMetrics,
    Freshness,
    GpuMetrics,
    HostMetrics,
)
from app.schemas.lifecycle import ApplicationStatus

from .ownership import GpuProcessOwnershipService


def _error_code(exc: BaseException) -> str:
    return type(exc).__name__.upper()


class HostMetricsCollector:
    def __init__(
        self,
        disk_paths: tuple[Path, ...] = settings.monitor_disks,
        psutil_module: object = psutil,
    ) -> None:
        self.disk_paths = disk_paths
        self.psutil = psutil_module

    def _filesystem(self, path: Path) -> str | None:
        try:
            configured = os.path.abspath(path)
            matches = []
            for partition in self.psutil.disk_partitions(all=True):
                mountpoint = os.path.abspath(partition.mountpoint)
                try:
                    if os.path.commonpath([configured, mountpoint]) == mountpoint:
                        matches.append((len(mountpoint), partition.fstype))
                except ValueError:
                    continue
            return max(matches, default=(0, None))[1]
        except (OSError, RuntimeError, psutil.Error):
            return None

    def _disks(self) -> tuple[list[DiskMetrics], CollectorState]:
        disks = []
        failures = 0
        for path in self.disk_paths:
            try:
                if not path.exists():
                    raise FileNotFoundError
                usage = self.psutil.disk_usage(str(path))
                disks.append(
                    DiskMetrics(
                        mountpoint=str(path),
                        filesystem=self._filesystem(path),
                        total_bytes=int(usage.total),
                        used_bytes=int(usage.used),
                        free_bytes=int(usage.free),
                        percent=float(usage.percent),
                    )
                )
            except (OSError, RuntimeError, psutil.Error):
                failures += 1
                disks.append(
                    DiskMetrics(
                        mountpoint=str(path),
                        available=False,
                        error="Configured path is unavailable",
                    )
                )
        if failures:
            return disks, CollectorState(
                available=failures < len(self.disk_paths),
                error_code="DISK_METRICS_PARTIAL" if failures < len(self.disk_paths) else "DISK_METRICS_UNAVAILABLE",
                message=f"{failures} configured disk path(s) are unavailable",
            )
        return disks, CollectorState(available=True)

    def collect(self) -> tuple[HostMetrics, dict[str, CollectorState]]:
        disks, disk_state = self._disks()
        try:
            per_core = [
                float(value)
                for value in self.psutil.cpu_percent(interval=0.1, percpu=True)
            ]
            cpu_percent = sum(per_core) / len(per_core) if per_core else 0.0
            memory = self.psutil.virtual_memory()
            swap = self.psutil.swap_memory()
            try:
                load_average = tuple(float(value) for value in self.psutil.getloadavg())
            except (AttributeError, OSError):
                load_average = None
            uptime = max(0.0, time.time() - float(self.psutil.boot_time()))
            metrics = HostMetrics(
                cpu_percent=cpu_percent,
                cpu_per_core_percent=per_core,
                load_average=load_average,
                memory_total_bytes=int(memory.total),
                memory_used_bytes=int(memory.used),
                memory_available_bytes=int(memory.available),
                memory_percent=float(memory.percent),
                swap_total_bytes=int(swap.total),
                swap_used_bytes=int(swap.used),
                swap_percent=float(swap.percent),
                disks=disks,
                uptime_seconds=uptime,
            )
            host_state = CollectorState(available=True)
        except (OSError, RuntimeError, psutil.Error) as exc:
            metrics = HostMetrics(disks=disks)
            host_state = CollectorState(
                available=False,
                error_code="HOST_METRICS_UNAVAILABLE",
                message=_error_code(exc),
            )
        return metrics, {"host": host_state, "disks": disk_state}


class GpuMetricsCollector:
    def __init__(
        self,
        module_loader: Callable[[str], object] = importlib.import_module,
        ownership: GpuProcessOwnershipService | None = None,
    ) -> None:
        self.module_loader = module_loader
        self.ownership = ownership or GpuProcessOwnershipService()
        self.module: object | None = None
        self.initialized = False

    def _initialize(self) -> CollectorState | None:
        if self.initialized:
            return None
        try:
            self.module = self.module_loader("pynvml")
            self.module.nvmlInit()
            self.initialized = True
            return None
        except Exception as exc:
            self.module = None
            self.initialized = False
            return CollectorState(
                available=False,
                error_code="NVML_NOT_AVAILABLE",
                message=_error_code(exc),
            )

    @staticmethod
    def _decode(value: object) -> str:
        return value.decode(errors="replace") if isinstance(value, bytes) else str(value)

    def _optional(self, name: str, *arguments: object) -> object | None:
        try:
            return getattr(self.module, name)(*arguments)
        except Exception:
            return None

    def _processes(self, handle: object) -> list[tuple[int, int | None]]:
        by_pid: dict[int, int | None] = {}
        for function_name in (
            "nvmlDeviceGetComputeRunningProcesses",
            "nvmlDeviceGetGraphicsRunningProcesses",
        ):
            running = self._optional(function_name, handle) or []
            for process in running:
                pid = int(process.pid)
                raw_used = getattr(process, "usedGpuMemory", None)
                used = int(raw_used) if isinstance(raw_used, int) and 0 <= raw_used < 2**63 else None
                by_pid[pid] = used if used is not None else by_pid.get(pid)
        return list(by_pid.items())

    def _device(self, index: int) -> GpuMetrics:
        try:
            handle = self.module.nvmlDeviceGetHandleByIndex(index)
            memory = self.module.nvmlDeviceGetMemoryInfo(handle)
            utilization = self._optional("nvmlDeviceGetUtilizationRates", handle)
            temperature = self._optional(
                "nvmlDeviceGetTemperature",
                handle,
                getattr(self.module, "NVML_TEMPERATURE_GPU", 0),
            )
            power_usage = self._optional("nvmlDeviceGetPowerUsage", handle)
            power_limit = self._optional("nvmlDeviceGetEnforcedPowerLimit", handle)
            fan = self._optional("nvmlDeviceGetFanSpeed", handle)
            processes = self.ownership.enrich(self._processes(handle))
            return GpuMetrics(
                index=index,
                uuid=self._decode(self.module.nvmlDeviceGetUUID(handle)),
                name=self._decode(self.module.nvmlDeviceGetName(handle)),
                utilization_percent=float(utilization.gpu) if utilization is not None else None,
                memory_total_bytes=int(memory.total),
                memory_used_bytes=int(memory.used),
                memory_free_bytes=int(memory.free),
                temperature_celsius=float(temperature) if temperature is not None else None,
                power_usage_watts=float(power_usage) / 1000 if power_usage is not None else None,
                power_limit_watts=float(power_limit) / 1000 if power_limit is not None else None,
                fan_percent=float(fan) if fan is not None else None,
                process_count=len(processes),
                processes=processes,
            )
        except Exception as exc:
            return GpuMetrics(
                index=index,
                uuid=f"unavailable-{index}",
                name=f"GPU {index}",
                memory_total_bytes=0,
                memory_used_bytes=0,
                memory_free_bytes=0,
                process_count=0,
                available=False,
                error=_error_code(exc),
            )

    def collect(self) -> tuple[list[GpuMetrics], CollectorState]:
        initialization_error = self._initialize()
        if initialization_error:
            return [], initialization_error
        try:
            count = int(self.module.nvmlDeviceGetCount())
            gpus = [self._device(index) for index in range(count)]
            failures = sum(not gpu.available for gpu in gpus)
            if failures:
                return gpus, CollectorState(
                    available=failures < count,
                    error_code="GPU_METRICS_PARTIAL",
                    message=f"{failures} GPU(s) could not be queried",
                )
            return gpus, CollectorState(available=True)
        except Exception as exc:
            self.close()
            return [], CollectorState(
                available=False,
                error_code="GPU_METRICS_UNAVAILABLE",
                message=_error_code(exc),
            )

    def close(self) -> None:
        if self.initialized and self.module is not None:
            try:
                self.module.nvmlShutdown()
            except Exception:
                pass
        self.initialized = False
        self.module = None


class ApplicationSummaryCollector:
    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    def collect(self) -> tuple[ApplicationSummary, CollectorState]:
        try:
            with self.session_factory() as session:
                applications = session.scalars(select(ApplicationRecord)).all()
                instances = session.scalars(
                    select(ApplicationInstanceRecord).order_by(
                        ApplicationInstanceRecord.created_at.desc()
                    )
                ).all()
            latest = {}
            for instance in instances:
                latest.setdefault(instance.application_id, instance.status)
            counts = {field: 0 for field in ApplicationSummary.model_fields if field != "total"}
            for application in applications:
                if not application.enabled:
                    counts["disabled"] += 1
                    continue
                status = latest.get(application.id, ApplicationStatus.STOPPED.value)
                if status == ApplicationStatus.RUNNING.value:
                    counts["running"] += 1
                elif status == ApplicationStatus.STOPPED.value:
                    counts["stopped"] += 1
                elif status in {
                    ApplicationStatus.STARTING.value,
                    ApplicationStatus.CHECKING.value,
                }:
                    counts["starting"] += 1
                elif status == ApplicationStatus.STOPPING.value:
                    counts["stopping"] += 1
                elif status == ApplicationStatus.UNHEALTHY.value:
                    counts["unhealthy"] += 1
                elif status == ApplicationStatus.FAILED.value:
                    counts["failed"] += 1
                elif status == ApplicationStatus.QUEUED.value:
                    counts["queued"] += 1
                else:
                    counts["unknown"] += 1
            return (
                ApplicationSummary(total=len(applications), **counts),
                CollectorState(available=True),
            )
        except Exception as exc:
            return ApplicationSummary(), CollectorState(
                available=False,
                error_code="APPLICATION_SUMMARY_UNAVAILABLE",
                message=_error_code(exc),
            )


class DashboardCollector:
    def __init__(
        self,
        host: HostMetricsCollector | None = None,
        gpu: GpuMetricsCollector | None = None,
        applications: ApplicationSummaryCollector | None = None,
        offload: bool = True,
    ) -> None:
        self.host = host or HostMetricsCollector()
        self.gpu = gpu or GpuMetricsCollector()
        self.applications = applications or ApplicationSummaryCollector()
        self.offload = offload

    async def _run_collector(self, collector: object) -> object:
        if self.offload:
            return await asyncio.to_thread(collector.collect)
        return collector.collect()

    async def collect(self) -> DashboardSnapshot:
        started = time.perf_counter()
        results = await asyncio.gather(
            self._run_collector(self.host),
            self._run_collector(self.gpu),
            self._run_collector(self.applications),
            return_exceptions=True,
        )
        if isinstance(results[0], BaseException):
            host = HostMetrics()
            host_states = {
                "host": CollectorState(
                    available=False,
                    error_code="HOST_METRICS_UNAVAILABLE",
                    message=_error_code(results[0]),
                ),
                "disks": CollectorState(
                    available=False, error_code="DISK_METRICS_UNAVAILABLE"
                ),
            }
        else:
            host, host_states = results[0]
        if isinstance(results[1], BaseException):
            gpus = []
            gpu_state = CollectorState(
                available=False,
                error_code="GPU_METRICS_UNAVAILABLE",
                message=_error_code(results[1]),
            )
        else:
            gpus, gpu_state = results[1]
        if isinstance(results[2], BaseException):
            applications = ApplicationSummary()
            application_state = CollectorState(
                available=False,
                error_code="APPLICATION_SUMMARY_UNAVAILABLE",
                message=_error_code(results[2]),
            )
        else:
            applications, application_state = results[2]
        return DashboardSnapshot(
            collected_at=datetime.now(timezone.utc),
            collection_duration_ms=(time.perf_counter() - started) * 1000,
            freshness=Freshness.LIVE,
            host=host,
            gpus=gpus,
            applications=applications,
            collectors={
                **host_states,
                "nvml": gpu_state,
                "applications": application_state,
            },
        )

    async def close(self) -> None:
        if self.offload:
            await asyncio.to_thread(self.gpu.close)
        else:
            self.gpu.close()
