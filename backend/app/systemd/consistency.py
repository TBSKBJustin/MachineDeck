from __future__ import annotations

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import ApplicationInstanceRecord, ApplicationRecord
from app.schemas.applications import ApplicationManifest, ProcessRuntime
from app.schemas.lifecycle import UnitConsistencyResponse

from .user_units import UserUnitManager


def check_application_unit(
    session: Session, application_id: str, manager: UserUnitManager | None = None
) -> UnitConsistencyResponse:
    record = session.get(ApplicationRecord, application_id)
    if record is None:
        raise ValueError(f"Application not found: {application_id}")
    manifest = ApplicationManifest.model_validate(yaml.safe_load(record.config_yaml))
    result = (manager or UserUnitManager()).consistency(manifest)
    state = session.scalar(
        select(ApplicationInstanceRecord)
        .where(ApplicationInstanceRecord.application_id == application_id)
        .order_by(ApplicationInstanceRecord.created_at.desc())
        .limit(1)
    )
    if state is not None:
        metadata = dict(state.metadata_json or {})
        metadata["unit_consistency"] = result.status.value
        metadata["unit_consistency_message"] = result.message
        state.metadata_json = metadata
        session.commit()
    return UnitConsistencyResponse(
        application_id=result.application_id,
        unit_name=result.unit_name,
        status=result.status.value,
        message=result.message,
    )


def reconcile_all_user_units(
    session: Session, manager: UserUnitManager | None = None
) -> list[UnitConsistencyResponse]:
    records = session.scalars(
        select(ApplicationRecord).where(ApplicationRecord.runtime_type == "process")
    ).all()
    results = []
    for record in records:
        manifest = ApplicationManifest.model_validate(yaml.safe_load(record.config_yaml))
        if isinstance(manifest.runtime, ProcessRuntime):
            results.append(check_application_unit(session, record.id, manager))
    return results
