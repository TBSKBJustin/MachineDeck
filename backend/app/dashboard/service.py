from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import datetime, timezone

from app.config import settings
from app.schemas.dashboard import DashboardSnapshot, Freshness

from .collectors import DashboardCollector


def snapshot_freshness(collected_at: datetime, now: datetime | None = None) -> Freshness:
    current = now or datetime.now(timezone.utc)
    timestamp = (
        collected_at.replace(tzinfo=timezone.utc)
        if collected_at.tzinfo is None
        else collected_at
    )
    age = max(0.0, (current - timestamp).total_seconds())
    if age <= 5:
        return Freshness.LIVE
    if age <= 15:
        return Freshness.STALE
    return Freshness.OFFLINE


class MetricsService:
    def __init__(
        self,
        collector: DashboardCollector | None = None,
        interval_seconds: float = settings.dashboard_interval_seconds,
    ) -> None:
        self.collector = collector or DashboardCollector()
        self.interval_seconds = max(0.1, interval_seconds)
        self._snapshot: DashboardSnapshot | None = None
        self._task: asyncio.Task[None] | None = None
        self._collection_lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[DashboardSnapshot | None]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self._collect_and_publish()
        self._task = asyncio.create_task(self._run(), name="dashboard-metrics-collector")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for queue in list(self._subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(None)
        await self.collector.close()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self._collect_and_publish()
            except Exception:
                # Preserve the last snapshot so freshness moves to STALE/OFFLINE.
                continue

    async def _collect_and_publish(self) -> DashboardSnapshot:
        async with self._collection_lock:
            snapshot = await self.collector.collect()
            self._snapshot = snapshot
            for queue in list(self._subscribers):
                if queue.full():
                    with suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait(snapshot)
            return snapshot

    async def latest(self) -> DashboardSnapshot:
        if self._snapshot is None:
            await self._collect_and_publish()
        return self._snapshot.model_copy(
            update={"freshness": snapshot_freshness(self._snapshot.collected_at)}
        )

    async def subscribe(self) -> AsyncIterator[DashboardSnapshot]:
        queue: asyncio.Queue[DashboardSnapshot | None] = asyncio.Queue(maxsize=1)
        snapshot = await self.latest()
        self._subscribers.add(queue)
        try:
            queue.put_nowait(snapshot)
            while True:
                snapshot = await queue.get()
                if snapshot is None:
                    return
                yield snapshot.model_copy(
                    update={"freshness": snapshot_freshness(snapshot.collected_at)}
                )
        finally:
            self._subscribers.discard(queue)


metrics_service = MetricsService()
