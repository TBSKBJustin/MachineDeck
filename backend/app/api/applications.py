from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.database.session import get_session
from app.orchestration.lifecycle_service import LifecycleError, LifecycleService, locks
from app.schemas.applications import ApplicationManifest, ApplicationResponse, ValidationResponse
from app.schemas.applications import validate_manifest_paths
from app.schemas.lifecycle import (
    ApplicationStateResponse,
    LifecycleActionResponse,
    LogResponse,
    UnitConsistencyResponse,
)
from app.services.applications import (
    ApplicationExistsError,
    ApplicationNotFoundError,
    ApplicationRunningError,
)
from app.services.applications import create_application as create_application_record
from app.services.applications import delete_application as delete_application_record
from app.services.applications import get_application as get_application_record
from app.services.applications import list_applications as list_application_records
from app.services.applications import update_application as update_application_record
from app.systemd.consistency import check_application_unit
from app.orchestration.ports import PortService, PortServiceError
from app.schemas.ports import EndpointsResponse, PortsResponse


router = APIRouter(prefix="/api/v1/applications", tags=["applications"])
DatabaseSession = Annotated[Session, Depends(get_session)]


def _validated(manifest: ApplicationManifest) -> ValidationResponse:
    result = validate_manifest_paths(manifest)
    if not result.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "CONFIG_INVALID", **result.model_dump()},
        )
    return result


def _lifecycle_error(exc: LifecycleError) -> HTTPException:
    status_code = (
        status.HTTP_404_NOT_FOUND if exc.code == "APP_NOT_FOUND" else status.HTTP_409_CONFLICT
    )
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": str(exc), "details": exc.details},
    )


def _port_error(exc: PortServiceError) -> HTTPException:
    status_code = status.HTTP_404_NOT_FOUND if exc.code == "APP_NOT_FOUND" else status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": str(exc), "details": exc.details},
    )


async def _reject_lifecycle_body(request: Request) -> None:
    if await request.body():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ARGUMENTS_NOT_ALLOWED",
                "message": "Lifecycle requests do not accept commands, units, paths, or arguments.",
            },
        )


@router.get("", response_model=list[ApplicationResponse])
async def list_applications(session: DatabaseSession) -> list[ApplicationResponse]:
    return list_application_records(session)


@router.post("", response_model=ApplicationResponse, status_code=status.HTTP_201_CREATED)
async def create_application(
    manifest: ApplicationManifest, session: DatabaseSession
) -> ApplicationResponse:
    _validated(manifest)
    try:
        return create_application_record(session, manifest)
    except ApplicationExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/validate", response_model=ValidationResponse)
async def validate_application(manifest: ApplicationManifest) -> ValidationResponse:
    return validate_manifest_paths(manifest)


@router.get("/{application_id}", response_model=ApplicationResponse)
async def get_application(application_id: str, session: DatabaseSession) -> ApplicationResponse:
    try:
        return get_application_record(session, application_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put("/{application_id}", response_model=ApplicationResponse)
async def update_application(
    application_id: str, manifest: ApplicationManifest, session: DatabaseSession
) -> ApplicationResponse:
    _validated(manifest)
    try:
        async with locks.get(application_id):
            return update_application_record(session, application_id, manifest)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ApplicationRunningError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/{application_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_application(application_id: str, session: DatabaseSession) -> Response:
    try:
        async with locks.get(application_id):
            delete_application_record(session, application_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ApplicationRunningError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{application_id}/validate", response_model=ValidationResponse)
async def validate_saved_application(application_id: str, session: DatabaseSession) -> ValidationResponse:
    try:
        application = get_application_record(session, application_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return validate_manifest_paths(application.manifest)


@router.post("/{application_id}/start", response_model=LifecycleActionResponse)
async def start_application(
    application_id: str, request: Request, session: DatabaseSession
) -> LifecycleActionResponse:
    await _reject_lifecycle_body(request)
    try:
        return await LifecycleService(session).action(application_id, "start")
    except LifecycleError as exc:
        raise _lifecycle_error(exc) from exc


@router.post("/{application_id}/stop", response_model=LifecycleActionResponse)
async def stop_application(
    application_id: str, request: Request, session: DatabaseSession
) -> LifecycleActionResponse:
    await _reject_lifecycle_body(request)
    try:
        return await LifecycleService(session).action(application_id, "stop")
    except LifecycleError as exc:
        raise _lifecycle_error(exc) from exc


@router.post("/{application_id}/restart", response_model=LifecycleActionResponse)
async def restart_application(
    application_id: str, request: Request, session: DatabaseSession
) -> LifecycleActionResponse:
    await _reject_lifecycle_body(request)
    try:
        return await LifecycleService(session).action(application_id, "restart")
    except LifecycleError as exc:
        raise _lifecycle_error(exc) from exc


@router.get("/{application_id}/status", response_model=ApplicationStateResponse)
async def application_status(
    application_id: str, session: DatabaseSession
) -> ApplicationStateResponse:
    try:
        return await LifecycleService(session).status(application_id)
    except LifecycleError as exc:
        raise _lifecycle_error(exc) from exc


@router.get("/{application_id}/logs", response_model=LogResponse)
async def application_logs(
    application_id: str,
    session: DatabaseSession,
    lines: int = Query(default=200, ge=1, le=5000),
) -> LogResponse:
    try:
        return await LifecycleService(session).logs(application_id, lines)
    except LifecycleError as exc:
        raise _lifecycle_error(exc) from exc


@router.get("/{application_id}/unit-consistency", response_model=UnitConsistencyResponse)
async def application_unit_consistency(
    application_id: str, session: DatabaseSession
) -> UnitConsistencyResponse:
    try:
        async with locks.get(application_id):
            return check_application_unit(session, application_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{application_id}/ports", response_model=PortsResponse)
async def application_ports(
    application_id: str,
    session: DatabaseSession,
) -> PortsResponse:
    try:
        return await PortService(session).ports(application_id)
    except PortServiceError as exc:
        raise _port_error(exc) from exc


@router.post("/{application_id}/ports/refresh", response_model=PortsResponse)
async def refresh_application_ports(
    application_id: str,
    request: Request,
    session: DatabaseSession,
) -> PortsResponse:
    await _reject_lifecycle_body(request)
    try:
        async with locks.get(application_id):
            return await PortService(session).ports(application_id)
    except PortServiceError as exc:
        raise _port_error(exc) from exc


@router.get("/{application_id}/endpoints", response_model=EndpointsResponse)
async def application_endpoints(
    application_id: str,
    session: DatabaseSession,
    scope: str = Query(default="local", pattern="^(local|lan)$"),
) -> EndpointsResponse:
    try:
        return await PortService(session).endpoints(application_id, scope)
    except PortServiceError as exc:
        raise _port_error(exc) from exc
