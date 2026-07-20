from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.applications import ApplicationManifest, validate_manifest_paths


def compose_manifest(project: Path, compose_file: str = "compose.yaml") -> dict:
    return {
        "version": 1,
        "id": "test-compose",
        "name": "Test Compose",
        "runtime": {
            "type": "compose",
            "working_dir": str(project),
            "compose_file": compose_file,
        },
        "ports": [{"id": "web", "name": "Web", "host": 8080}],
    }


def test_compose_manifest_and_paths_validate(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    manifest = ApplicationManifest.model_validate(compose_manifest(tmp_path))
    result = validate_manifest_paths(manifest, allowed_roots=(tmp_path,))
    assert result.valid
    assert not result.errors


def test_compose_path_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="without path components"):
        ApplicationManifest.model_validate(compose_manifest(tmp_path, "../compose.yaml"))


def test_unknown_command_field_is_rejected(tmp_path: Path) -> None:
    data = compose_manifest(tmp_path)
    data["start_command"] = "docker compose up -d; reboot"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ApplicationManifest.model_validate(data)


def test_process_executable_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="executable must be an absolute path"):
        ApplicationManifest.model_validate(
            {
                "id": "unsafe-process",
                "name": "Unsafe",
                "runtime": {
                    "type": "process",
                    "working_dir": str(tmp_path),
                    "command": ["python", "app.py"],
                },
            }
        )


def test_working_directory_outside_allowlist_fails_validation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    manifest = ApplicationManifest.model_validate(compose_manifest(project))
    result = validate_manifest_paths(manifest, allowed_roots=(allowed,))
    assert not result.valid
    assert result.errors[0].code == "PATH_NOT_ALLOWED"
