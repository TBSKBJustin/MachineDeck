from __future__ import annotations

from uuid import uuid4

import yaml
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import ApplicationInstanceRecord, ApplicationRecord, AuditEventRecord
from app.schemas.applications import ApplicationManifest, ApplicationResponse
from app.schemas.lifecycle import ApplicationStatus


class ApplicationExistsError(ValueError):
    pass


class ApplicationNotFoundError(ValueError):
    pass


def _serialize_manifest(manifest: ApplicationManifest) -> str:
    return yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False)


def _response(record: ApplicationRecord, session: Session) -> ApplicationResponse:
    manifest = ApplicationManifest.model_validate(yaml.safe_load(record.config_yaml))
    latest_state = session.scalar(
        select(ApplicationInstanceRecord)
        .where(ApplicationInstanceRecord.application_id == record.id)
        .order_by(ApplicationInstanceRecord.created_at.desc())
        .limit(1)
    )
    current_status = (
        ApplicationStatus.DISABLED
        if not record.enabled
        else ApplicationStatus(latest_state.status) if latest_state else ApplicationStatus.STOPPED
    )
    return ApplicationResponse(
        id=record.id,
        name=record.name,
        description=record.description,
        runtime_type=record.runtime_type,
        enabled=record.enabled,
        status=current_status,
        manifest=manifest,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _audit(
    session: Session,
    action: str,
    target_id: str,
    result: str = "success",
    details: dict | None = None,
) -> None:
    session.add(
        AuditEventRecord(
            id=str(uuid4()),
            actor="phase1-api",
            action=action,
            target_type="application",
            target_id=target_id,
            result=result,
            details_json=details or {},
        )
    )


def list_applications(session: Session) -> list[ApplicationResponse]:
    records = session.scalars(select(ApplicationRecord).order_by(ApplicationRecord.name)).all()
    return [_response(record, session) for record in records]


def get_application(session: Session, application_id: str) -> ApplicationResponse:
    record = session.get(ApplicationRecord, application_id)
    if record is None:
        raise ApplicationNotFoundError(f"Application not found: {application_id}")
    return _response(record, session)


def create_application(session: Session, manifest: ApplicationManifest) -> ApplicationResponse:
    if session.get(ApplicationRecord, manifest.id) is not None:
        raise ApplicationExistsError(f"Application already exists: {manifest.id}")
    record = ApplicationRecord(
        id=manifest.id,
        name=manifest.name,
        description=manifest.description,
        runtime_type=manifest.runtime.type,
        config_yaml=_serialize_manifest(manifest),
        enabled=manifest.enabled,
    )
    session.add(record)
    session.add(
        ApplicationInstanceRecord(
            id=str(uuid4()),
            application_id=manifest.id,
            status=(ApplicationStatus.STOPPED if manifest.enabled else ApplicationStatus.DISABLED).value,
            metadata_json={},
        )
    )
    _audit(session, "application.create", manifest.id, details={"runtime_type": manifest.runtime.type})
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ApplicationExistsError(f"Application already exists: {manifest.id}") from exc
    session.refresh(record)
    return _response(record, session)


def update_application(
    session: Session, application_id: str, manifest: ApplicationManifest
) -> ApplicationResponse:
    if application_id != manifest.id:
        raise ValueError("Manifest id must match the application id in the URL")
    record = session.get(ApplicationRecord, application_id)
    if record is None:
        raise ApplicationNotFoundError(f"Application not found: {application_id}")
    record.name = manifest.name
    record.description = manifest.description
    record.runtime_type = manifest.runtime.type
    record.config_yaml = _serialize_manifest(manifest)
    record.enabled = manifest.enabled
    _audit(session, "application.update", application_id, details={"runtime_type": manifest.runtime.type})
    session.commit()
    session.refresh(record)
    return _response(record, session)


def delete_application(session: Session, application_id: str) -> None:
    record = session.get(ApplicationRecord, application_id)
    if record is None:
        raise ApplicationNotFoundError(f"Application not found: {application_id}")
    session.delete(record)
    _audit(session, "application.delete", application_id)
    session.commit()
