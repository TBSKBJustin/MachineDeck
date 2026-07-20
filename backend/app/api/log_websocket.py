from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.database.session import SessionLocal
from app.logging.adapters import LogSourceError
from app.logging.models import LogEnvelope, LogEvent
from app.logging.service import ApplicationLogError, ApplicationLogService, parse_service_filter
from app.security.auth import authenticate_websocket, wait_for_websocket_session_end


router = APIRouter()
LOG_QUEUE_SIZE = 1000
ALLOWED_LOG_QUERY_PARAMETERS = {"history", "follow", "since", "cursor", "services"}


class LogConnectionRegistry:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    def add(self, websocket: WebSocket) -> None:
        self.connections.add(websocket)

    def discard(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    async def close_all(self) -> None:
        connections = list(self.connections)
        self.connections.clear()
        await asyncio.gather(
            *(connection.close(code=1012, reason="MachineDeck is shutting down") for connection in connections),
            return_exceptions=True,
        )


log_connections = LogConnectionRegistry()


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ApplicationLogError("QUERY_INVALID", f"Invalid boolean value: {value}")


def _parse_query(
    websocket: WebSocket,
) -> tuple[int, bool, datetime | None, str | None, set[str]]:
    query = websocket.query_params
    unknown = set(query.keys()) - ALLOWED_LOG_QUERY_PARAMETERS
    if unknown:
        raise ApplicationLogError(
            "QUERY_INVALID",
            f"Unsupported log query parameters: {', '.join(sorted(unknown))}",
        )
    try:
        history = int(query.get("history", "200"))
    except ValueError as exc:
        raise ApplicationLogError("QUERY_INVALID", "history must be an integer") from exc
    if not 0 <= history <= 5000:
        raise ApplicationLogError("QUERY_INVALID", "history must be between 0 and 5000")
    follow = _parse_bool(query.get("follow"), True)
    since_text = query.get("since")
    try:
        since = datetime.fromisoformat(since_text.replace("Z", "+00:00")) if since_text else None
    except ValueError as exc:
        raise ApplicationLogError("QUERY_INVALID", "since must be an ISO-8601 timestamp") from exc
    if since is not None and since.tzinfo is None:
        raise ApplicationLogError("QUERY_INVALID", "since must include a timezone")
    cursor = query.get("cursor")
    if cursor is not None and (
        not cursor or len(cursor) > 2048 or any(char in cursor for char in ("\n", "\r", "\x00"))
    ):
        raise ApplicationLogError("QUERY_INVALID", "Invalid journal cursor")
    return history, follow, since, cursor, parse_service_filter(query.get("services"))


async def _send(websocket: WebSocket, envelope: LogEnvelope) -> None:
    await websocket.send_json(envelope.model_dump(mode="json"))


def enqueue_bounded(
    queue: asyncio.Queue[LogEvent | LogEnvelope],
    item: LogEvent | LogEnvelope,
    dropped: list[int],
) -> None:
    if queue.full():
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()
            dropped[0] += 1
    queue.put_nowait(item)


@router.websocket("/ws/v1/applications/{application_id}/logs")
async def application_logs(websocket: WebSocket, application_id: str) -> None:
    auth_session_id = await authenticate_websocket(websocket)
    if auth_session_id is None:
        return
    await websocket.accept()
    log_connections.add(websocket)
    producer: asyncio.Task[None] | None = None
    disconnect: asyncio.Task[dict] | None = None
    auth_expiration = asyncio.create_task(
        wait_for_websocket_session_end(auth_session_id)
    )
    try:
        history_limit, should_follow, since, requested_cursor, services = _parse_query(websocket)
        await _send(websocket, LogEnvelope(type="status", data={"state": "connected"}))
        with SessionLocal() as session:
            service = ApplicationLogService(session)
            history, adapter, redaction = await service.history(
                application_id,
                limit=history_limit,
                since=since,
                cursor=requested_cursor,
                services=services,
            )
            sequence = 0
            for event in history:
                sequence += 1
                event = event.model_copy(update={"sequence": sequence})
                await _send(
                    websocket,
                    LogEnvelope(type="log", data=event.model_dump(mode="json")),
                )
            if not should_follow:
                await _send(websocket, LogEnvelope(type="eof", data={"reason": "history_complete"}))
                return

            cursor = next(
                (event.cursor for event in reversed(history) if event.cursor), requested_cursor
            )
            queue: asyncio.Queue[LogEvent | LogEnvelope] = asyncio.Queue(maxsize=LOG_QUEUE_SIZE)
            dropped = [0]

            async def produce() -> None:
                try:
                    async for event in service.follow(
                        adapter, redaction, cursor=cursor, services=services
                    ):
                        enqueue_bounded(queue, event, dropped)
                    enqueue_bounded(
                        queue,
                        LogEnvelope(type="eof", data={"reason": "log_source_closed"}),
                        dropped,
                    )
                except LogSourceError as exc:
                    enqueue_bounded(
                        queue,
                        LogEnvelope(
                            type="error",
                            data={"code": "LOG_SOURCE_UNAVAILABLE", "message": str(exc)},
                        ),
                        dropped,
                    )
                except asyncio.CancelledError:
                    raise

            producer = asyncio.create_task(produce())
            disconnect = asyncio.create_task(websocket.receive())
            while True:
                queued = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {queued, disconnect, auth_expiration},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if auth_expiration in done:
                    queued.cancel()
                    with suppress(asyncio.CancelledError):
                        await queued
                    with suppress(WebSocketDisconnect, RuntimeError):
                        await websocket.close(code=4401, reason="Session expired or revoked")
                    break
                if disconnect in done:
                    queued.cancel()
                    with suppress(asyncio.CancelledError):
                        await queued
                    break
                item = queued.result()
                if dropped[0]:
                    count = dropped[0]
                    dropped[0] = 0
                    await _send(
                        websocket,
                        LogEnvelope(
                            type="warning",
                            data={"code": "LOG_MESSAGES_DROPPED", "count": count},
                        ),
                    )
                if isinstance(item, LogEvent):
                    sequence += 1
                    item = item.model_copy(update={"sequence": sequence})
                    await _send(
                        websocket,
                        LogEnvelope(type="log", data=item.model_dump(mode="json")),
                    )
                else:
                    await _send(websocket, item)
                    if item.type in {"error", "eof"}:
                        break
                if disconnect.done():
                    break
    except (ApplicationLogError, LogSourceError) as exc:
        code = exc.code if isinstance(exc, ApplicationLogError) else "LOG_SOURCE_UNAVAILABLE"
        with suppress(WebSocketDisconnect, RuntimeError):
            await _send(websocket, LogEnvelope(type="error", data={"code": code, "message": str(exc)}))
    except WebSocketDisconnect:
        pass
    finally:
        if not auth_expiration.done():
            auth_expiration.cancel()
            with suppress(asyncio.CancelledError):
                await auth_expiration
        if disconnect is not None and not disconnect.done():
            disconnect.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect
        if producer is not None and not producer.done():
            producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer
        log_connections.discard(websocket)
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()
