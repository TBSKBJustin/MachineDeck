from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_initial_migration_upgrades_empty_database(tmp_path: Path) -> None:
    database = tmp_path / "migration.db"
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")
    tables = set(inspect(create_engine(f"sqlite:///{database}")).get_table_names())
    assert {
        "alembic_version",
        "applications",
        "application_instances",
        "audit_events",
        "executions",
        "administrators",
        "auth_sessions",
        "login_failures",
    } <= tables
