import asyncio

import pytest
from starlette.datastructures import QueryParams

from app.api.log_websocket import (
    LogConnectionRegistry,
    _parse_query,
    enqueue_bounded,
)
from app.logging.models import LogEvent, LogEnvelope
from app.logging.service import ApplicationLogError


class QueryWebSocket:
    def __init__(self, query: str) -> None:
        self.query_params = QueryParams(query)


def test_log_query_rejects_arbitrary_unit_and_compose_paths() -> None:
    with pytest.raises(ApplicationLogError, match="Unsupported"):
        _parse_query(QueryWebSocket("unit=ssh.service&compose_file=/tmp/evil.yaml"))


def test_log_query_validates_ranges_timestamps_and_services() -> None:
    history, follow, since, cursor, services = _parse_query(
        QueryWebSocket(
            "history=25&follow=false&since=2026-07-20T12:00:00Z&cursor=s%3Dabc&services=api,worker"
        )
    )
    assert history == 25
    assert follow is False
    assert since is not None and since.tzinfo is not None
    assert cursor == "s=abc"
    assert services == {"api", "worker"}


def test_bounded_queue_discards_oldest_and_counts_drops() -> None:
    queue: asyncio.Queue[LogEvent | LogEnvelope] = asyncio.Queue(maxsize=2)
    dropped = [0]
    for message in ("one", "two", "three"):
        enqueue_bounded(
            queue,
            LogEvent(application_id="app", source="journal", message=message),
            dropped,
        )
    assert dropped == [1]
    assert queue.get_nowait().message == "two"
    assert queue.get_nowait().message == "three"


@pytest.mark.asyncio
async def test_shutdown_closes_all_registered_websockets() -> None:
    class Connection:
        def __init__(self) -> None:
            self.closed = False

        async def close(self, **_: object) -> None:
            self.closed = True

    registry = LogConnectionRegistry()
    first = Connection()
    second = Connection()
    registry.add(first)
    registry.add(second)
    await registry.close_all()
    assert first.closed and second.closed
    assert not registry.connections
