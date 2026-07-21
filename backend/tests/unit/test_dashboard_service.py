from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.dashboard.service import MetricsService, snapshot_freshness
from app.schemas.dashboard import (
    ApplicationSummary,
    CollectorState,
    DashboardSnapshot,
    Freshness,
    HostMetrics,
)


class FakeCollector:
    def __init__(self) -> None:
        self.count = 0
        self.closed = False

    async def collect(self) -> DashboardSnapshot:
        self.count += 1
        return DashboardSnapshot(
            collected_at=datetime.now(timezone.utc),
            collection_duration_ms=1,
            freshness=Freshness.LIVE,
            host=HostMetrics(),
            gpus=[],
            applications=ApplicationSummary(total=self.count),
            collectors={"host": CollectorState(available=True)},
        )

    async def close(self) -> None:
        self.closed = True


def test_snapshot_freshness_thresholds() -> None:
    now = datetime.now(timezone.utc)
    assert snapshot_freshness(now - timedelta(seconds=5), now) == Freshness.LIVE
    assert snapshot_freshness(now - timedelta(seconds=10), now) == Freshness.STALE
    assert snapshot_freshness(now - timedelta(seconds=16), now) == Freshness.OFFLINE


@pytest.mark.asyncio
async def test_subscribers_share_one_loop_get_immediate_latest_and_clean_up() -> None:
    collector = FakeCollector()
    service = MetricsService(collector, interval_seconds=0.1)
    await service.start()
    first = service.subscribe().__aiter__()
    second = service.subscribe().__aiter__()
    assert (await anext(first)).applications.total == 1
    assert (await anext(second)).applications.total == 1
    assert service.subscriber_count == 2
    await asyncio.sleep(0.12)
    assert collector.count == 2
    assert (await anext(first)).applications.total == 2
    assert (await anext(second)).applications.total == 2
    await first.aclose()
    await second.aclose()
    assert service.subscriber_count == 0
    await service.stop()
    assert collector.closed


@pytest.mark.asyncio
async def test_slow_subscriber_gets_newest_snapshot_without_blocking_collection() -> None:
    collector = FakeCollector()
    service = MetricsService(collector, interval_seconds=0.1)
    await service.start()
    subscriber = service.subscribe().__aiter__()
    await anext(subscriber)
    await asyncio.sleep(0.32)
    assert collector.count >= 4
    newest = await anext(subscriber)
    assert newest.applications.total == collector.count
    await subscriber.aclose()
    await service.stop()


@pytest.mark.asyncio
async def test_collector_continues_without_subscribers_and_shutdown_ends_stream() -> None:
    collector = FakeCollector()
    service = MetricsService(collector, interval_seconds=0.1)
    await service.start()
    await asyncio.sleep(0.12)
    assert collector.count == 2
    subscriber = service.subscribe().__aiter__()
    await anext(subscriber)
    await service.stop()
    with pytest.raises(StopAsyncIteration):
        await anext(subscriber)
