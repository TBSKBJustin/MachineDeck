from __future__ import annotations

import psutil

from app.schemas.dashboard import GpuProcessMetrics


class GpuProcessOwnershipService:
    """Keep GPU process enrichment independent from dashboard presentation."""

    def enrich(
        self,
        processes: list[tuple[int, int | None]],
        managed_pid_map: dict[int, str] | None = None,
    ) -> list[GpuProcessMetrics]:
        ownership = managed_pid_map or {}
        output = []
        for pid, used_vram_bytes in processes:
            try:
                process_name = psutil.Process(pid).name()
            except (psutil.Error, OSError):
                process_name = None
            application_id = ownership.get(pid)
            output.append(
                GpuProcessMetrics(
                    pid=pid,
                    used_vram_bytes=used_vram_bytes,
                    process_name=process_name,
                    application_id=application_id,
                    managed=application_id is not None,
                )
            )
        return output
