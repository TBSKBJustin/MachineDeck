from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database.base import Base
from app.database.models import ApplicationInstanceRecord
from app.schemas.applications import ApplicationManifest
from app.services.applications import create_application
from app.systemd.consistency import reconcile_all_user_units
from app.systemd.user_units import UserUnitManager, render_user_unit


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "process"


def test_reconciliation_persists_missing_and_matching_unit_state(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    manager = UserUnitManager(tmp_path / "units")
    manifest = ApplicationManifest.model_validate(
        {
            "id": "reconciled-app",
            "name": "Reconciled",
            "runtime": {
                "type": "process",
                "working_dir": str(FIXTURE_DIR),
                "command": [str(FIXTURE_DIR / "example.sh")],
            },
        }
    )
    with Session(engine, expire_on_commit=False) as session:
        create_application(session, manifest)
        first = reconcile_all_user_units(session, manager)
        assert first[0].status == "MISSING"

        target = manager.target_path(manifest.id, create_directory=True)
        target.write_text(render_user_unit(manifest), encoding="utf-8")
        target.chmod(0o644)
        second = reconcile_all_user_units(session, manager)
        assert second[0].status == "MATCH"

        state = session.scalar(
            select(ApplicationInstanceRecord).where(
                ApplicationInstanceRecord.application_id == manifest.id
            )
        )
        assert state is not None
        assert state.metadata_json["unit_consistency"] == "MATCH"
