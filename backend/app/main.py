from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.api.applications import router as applications_router
from app.api.log_websocket import log_connections, router as log_websocket_router
from app.database.session import create_schema
from app.phase0.report import collect_report
from app.systemd.consistency import reconcile_all_user_units


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    create_schema()
    from app.database.session import SessionLocal

    with SessionLocal() as session:
        reconcile_all_user_units(session)
    yield
    await log_connections.close_all()


app = FastAPI(title="MachineDeck", version="0.1.0", lifespan=lifespan)
app.include_router(applications_router)
app.include_router(log_websocket_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "phase": "1"}


@app.get("/api/v1/system/overview")
async def system_overview() -> dict:
    return await asyncio.to_thread(collect_report)


@app.websocket("/ws/system-metrics")
async def system_metrics(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(await asyncio.to_thread(collect_report))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return
