from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.session import get_session
from app.orchestration.port_discovery import PortDiscoveryError
from app.orchestration.ports import PortService
from app.schemas.ports import SystemPortsResponse


router = APIRouter(prefix="/api/v1/system", tags=["system"])
DatabaseSession = Annotated[Session, Depends(get_session)]


@router.get("/ports", response_model=SystemPortsResponse)
async def system_ports(session: DatabaseSession) -> SystemPortsResponse:
    try:
        return await PortService(session).system_ports()
    except PortDiscoveryError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "PORT_DISCOVERY_UNAVAILABLE", "message": str(exc)},
        ) from exc
