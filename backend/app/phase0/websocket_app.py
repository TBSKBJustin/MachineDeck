from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from .report import collect_report
from .registry import ApplicationNotFoundError, ApplicationRegistry, RegistryError
from .service_manager import LifecycleRouter

app = FastAPI(title="MachineDeck Phase 0", version="0.1.0")
registry_path = Path(__file__).resolve().parents[2] / "config" / "phase0-applications.yaml"
registry = ApplicationRegistry.load(registry_path)
lifecycle = LifecycleRouter(registry)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/phase0/report")
async def report() -> dict:
    return await asyncio.to_thread(collect_report)


@app.get("/api/phase0/applications")
async def applications() -> list[dict]:
    return [
        {"id": application.id, "name": application.name, "runtime_type": application.runtime_type}
        for application in registry.list()
    ]


async def _perform_action(application_id: str, action: str) -> dict:
    try:
        manager = lifecycle.for_application(application_id)
        result = await getattr(manager, action)(application_id)
        return asdict(result)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RegistryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _reject_request_arguments(request: Request) -> None:
    if await request.body():
        raise HTTPException(
            status_code=400,
            detail="Lifecycle requests do not accept commands, units, paths, or arguments",
        )


@app.post("/api/phase0/applications/{application_id}/start")
async def start_application(application_id: str, request: Request) -> dict:
    await _reject_request_arguments(request)
    return await _perform_action(application_id, "start")


@app.post("/api/phase0/applications/{application_id}/stop")
async def stop_application(application_id: str, request: Request) -> dict:
    await _reject_request_arguments(request)
    return await _perform_action(application_id, "stop")


@app.post("/api/phase0/applications/{application_id}/restart")
async def restart_application(application_id: str, request: Request) -> dict:
    await _reject_request_arguments(request)
    return await _perform_action(application_id, "restart")


@app.get("/api/phase0/applications/{application_id}/status")
async def application_status(application_id: str) -> dict:
    return await _perform_action(application_id, "status")


@app.get("/api/phase0/applications/{application_id}/logs")
async def application_logs(application_id: str, lines: int = Query(200, ge=1, le=5000)) -> dict:
    try:
        manager = lifecycle.for_application(application_id)
        return {"application_id": application_id, "lines": await manager.logs(application_id, lines)}
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RegistryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.websocket("/ws/system-metrics")
async def system_metrics(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            payload = await asyncio.to_thread(collect_report)
            await websocket.send_json(payload)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return
