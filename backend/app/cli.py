from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        deployment = Path(sys.prefix).parent / "scripts" / "machinedeck_deploy.py"
        if not deployment.is_file():
            raise SystemExit("MachineDeck doctor is available only in a managed installation")
        raise SystemExit(
            subprocess.call([sys.executable, str(deployment), "doctor", *sys.argv[2:]])
        )

    from app.config import settings
    import uvicorn

    uvicorn.run("app.main:app", host=settings.bind_host, port=settings.bind_port)


if __name__ == "__main__":
    main()
