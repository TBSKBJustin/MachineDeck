from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.dashboard.service import metrics_service
from app.schemas.dashboard import DashboardEnvelope, DashboardSnapshot
from app.security.auth import (
    AuthenticatedSession,
    authenticate_websocket,
    require_http_auth,
    wait_for_websocket_session_end,
)


router = APIRouter()


@router.get("/api/v1/dashboard", response_model=DashboardSnapshot, tags=["dashboard"])
async def dashboard_snapshot(
    _: Annotated[AuthenticatedSession, Depends(require_http_auth)],
) -> DashboardSnapshot:
    return await metrics_service.latest()


async def _dashboard_stream(websocket: WebSocket) -> None:
    auth_session_id = await authenticate_websocket(websocket)
    if auth_session_id is None:
        return
    if websocket.query_params:
        await websocket.close(code=4400, reason="Dashboard WebSocket does not accept query parameters")
        return
    await websocket.accept()
    expiration = asyncio.create_task(wait_for_websocket_session_end(auth_session_id))
    iterator = metrics_service.subscribe().__aiter__()
    try:
        while True:
            snapshot_task = asyncio.create_task(anext(iterator))
            done, _ = await asyncio.wait(
                {snapshot_task, expiration}, return_when=asyncio.FIRST_COMPLETED
            )
            if expiration in done:
                snapshot_task.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await snapshot_task
                await websocket.close(code=4401, reason="Session expired or revoked")
                return
            try:
                snapshot = snapshot_task.result()
            except StopAsyncIteration:
                await websocket.close(code=1012, reason="Dashboard collector stopped")
                return
            await websocket.send_json(
                DashboardEnvelope(data=snapshot).model_dump(mode="json")
            )
    except WebSocketDisconnect:
        return
    finally:
        if not expiration.done():
            expiration.cancel()
            with suppress(asyncio.CancelledError):
                await expiration
        with suppress(RuntimeError, WebSocketDisconnect):
            await iterator.aclose()


@router.websocket("/ws/v1/dashboard")
async def dashboard_websocket(websocket: WebSocket) -> None:
    await _dashboard_stream(websocket)


@router.websocket("/ws/system-metrics")
async def legacy_dashboard_websocket(websocket: WebSocket) -> None:
    await _dashboard_stream(websocket)
