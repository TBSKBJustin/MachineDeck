from __future__ import annotations

from .models import ProbeResult


def _decode(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def read_gpu_metrics() -> ProbeResult:
    """Read NVIDIA GPU and compute-process VRAM data through NVML."""
    try:
        import pynvml
    except ImportError:
        return ProbeResult("gpu_metrics", False, error="nvidia-ml-py is not installed")

    initialized = False
    try:
        pynvml.nvmlInit()
        initialized = True
        driver_version = _decode(pynvml.nvmlSystemGetDriverVersion())
        gpus = []
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
            processes = []
            try:
                running = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
            except pynvml.NVMLError_NotSupported:
                running = []
            for process in running:
                used = getattr(process, "usedGpuMemory", None)
                processes.append({"pid": process.pid, "used_vram_bytes": used})
            gpus.append(
                {
                    "index": index,
                    "name": _decode(pynvml.nvmlDeviceGetName(handle)),
                    "uuid": _decode(pynvml.nvmlDeviceGetUUID(handle)),
                    "memory": {
                        "total_bytes": memory.total,
                        "used_bytes": memory.used,
                        "free_bytes": memory.free,
                    },
                    "utilization": {
                        "gpu_percent": utilization.gpu,
                        "memory_percent": utilization.memory,
                    },
                    "processes": processes,
                }
            )
        warnings = []
        if len(gpus) < 2:
            warnings.append(f"Phase 0 acceptance expects two GPUs; NVML reported {len(gpus)}")
        return ProbeResult(
            "gpu_metrics", True, data={"driver_version": driver_version, "gpus": gpus}, warnings=warnings
        )
    except pynvml.NVMLError as exc:
        return ProbeResult("gpu_metrics", False, error=str(exc))
    finally:
        if initialized:
            pynvml.nvmlShutdown()

