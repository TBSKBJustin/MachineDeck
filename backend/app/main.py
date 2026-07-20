from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.applications import router as applications_router
from app.api.auth import router as auth_router
from app.api.log_websocket import log_connections, router as log_websocket_router
from app.api.system import router as system_router
from app.database.session import create_schema
from app.phase0.report import collect_report
from app.security.auth import (
    AuthenticatedSession,
    authenticate_websocket,
    require_http_auth,
    websocket_session_active,
)
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
app.include_router(auth_router)
app.include_router(applications_router, dependencies=[Depends(require_http_auth)])
app.include_router(log_websocket_router)
app.include_router(system_router, dependencies=[Depends(require_http_auth)])


@app.exception_handler(RequestValidationError)
async def scrub_auth_validation_errors(
    request: Request, exc: RequestValidationError
) -> Response:
    if not request.url.path.startswith("/api/v1/auth/"):
        return await request_validation_exception_handler(request, exc)
    errors = []
    for error in exc.errors():
        errors.append(
            {
                key: value
                for key, value in error.items()
                if key not in {"input", "ctx", "url"}
            }
        )
    return JSONResponse(status_code=422, content={"detail": errors})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "phase": "1"}


@app.get("/api/v1/system/overview")
async def system_overview(
    _: Annotated[AuthenticatedSession, Depends(require_http_auth)],
) -> dict:
    return await asyncio.to_thread(collect_report)


@app.websocket("/ws/system-metrics")
async def system_metrics(websocket: WebSocket) -> None:
    auth_session_id = await authenticate_websocket(websocket)
    if auth_session_id is None:
        return
    await websocket.accept()
    try:
        while True:
            if not websocket_session_active(auth_session_id):
                await websocket.close(code=4401, reason="Session expired or revoked")
                return
            await websocket.send_json(await asyncio.to_thread(collect_report))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return
