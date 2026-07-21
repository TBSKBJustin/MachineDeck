from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.applications import router as applications_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.dashboard import router as dashboard_router
from app.api.log_websocket import log_connections, router as log_websocket_router
from app.api.system import router as system_router
from app.database.session import create_schema
from app.dashboard.service import metrics_service
from app.config import PROJECT_ROOT
from app.schemas.dashboard import DashboardSnapshot
from app.security.auth import require_http_auth
from app.systemd.consistency import reconcile_all_user_units


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    create_schema()
    from app.database.session import SessionLocal

    with SessionLocal() as session:
        reconcile_all_user_units(session)
    await metrics_service.start()
    yield
    await metrics_service.stop()
    await log_connections.close_all()


app = FastAPI(title="MachineDeck", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(applications_router, dependencies=[Depends(require_http_auth)])
app.include_router(log_websocket_router)
app.include_router(system_router, dependencies=[Depends(require_http_auth)])
app.include_router(dashboard_router)
app.include_router(audit_router, dependencies=[Depends(require_http_auth)])


@app.middleware("http")
async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self' ws: wss:; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


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


@app.get("/api/v1/system/overview", dependencies=[Depends(require_http_auth)])
async def system_overview() -> DashboardSnapshot:
    return await metrics_service.latest()


app.mount(
    "/",
    StaticFiles(directory=PROJECT_ROOT / "frontend", html=True),
    name="frontend",
)
