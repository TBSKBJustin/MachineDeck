from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent


def _allowed_roots() -> tuple[Path, ...]:
    configured = os.getenv("MACHINEDECK_ALLOWED_ROOTS")
    values = configured.split(os.pathsep) if configured else [str(PROJECT_ROOT.parent)]
    return tuple(Path(value).expanduser().resolve() for value in values if value)


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "MACHINEDECK_DATABASE_URL", f"sqlite:///{BACKEND_ROOT / 'data' / 'machinedeck.db'}"
    )
    allowed_roots: tuple[Path, ...] = _allowed_roots()
    bind_host: str = os.getenv("MACHINEDECK_BIND_HOST", "127.0.0.1")
    bind_port: int = int(os.getenv("MACHINEDECK_BIND_PORT", "8080"))
    user_unit_dir: Path = Path(
        os.getenv(
            "MACHINEDECK_USER_UNIT_DIR",
            str(Path.home() / ".config" / "systemd" / "user"),
        )
    ).expanduser()


settings = Settings()
