from pathlib import Path

import pytest

from app.phase0.registry import ApplicationNotFoundError, ApplicationRegistry, RegistryError


def write_registry(tmp_path: Path, application_yaml: str) -> Path:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    registry = tmp_path / "registry.yaml"
    applications = "applications: []\n" if application_yaml.strip() == "[]" else f"applications:\n{application_yaml}"
    registry.write_text(f"allowed_roots:\n  - {fixture}\n{applications}", encoding="utf-8")
    return registry


def test_unregistered_application_is_rejected(tmp_path: Path) -> None:
    registry = ApplicationRegistry.load(write_registry(tmp_path, "[]\n"))
    with pytest.raises(ApplicationNotFoundError, match="not registered"):
        registry.get("unknown")


@pytest.mark.parametrize(
    "unit",
    [
        "ssh.service",
        "machinedeck-example.service --no-block",
        "machinedeck-../../ssh.service",
        "machinedeck-foo.service;reboot",
        "machinedeck-foo@bar.service",
    ],
)
def test_invalid_or_injected_unit_name_is_rejected(tmp_path: Path, unit: str) -> None:
    path = write_registry(
        tmp_path,
        f"  - id: example\n    name: Example\n    runtime:\n      type: systemd-user\n      unit: {unit!r}\n",
    )
    with pytest.raises(RegistryError, match="unsafe unit"):
        ApplicationRegistry.load(path)


@pytest.mark.parametrize("compose_file", ["../compose.yaml", "/tmp/compose.yaml", "foo/compose.yaml"])
def test_compose_file_path_traversal_is_rejected(tmp_path: Path, compose_file: str) -> None:
    fixture = tmp_path / "fixture"
    path = write_registry(
        tmp_path,
        "  - id: example\n"
        "    name: Example\n"
        "    runtime:\n"
        "      type: docker-compose\n"
        f"      project_directory: {fixture}\n"
        f"      compose_file: {compose_file!r}\n",
    )
    with pytest.raises(RegistryError, match="unsafe compose_file"):
        ApplicationRegistry.load(path)


def test_project_path_outside_allowlist_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    path = write_registry(
        tmp_path,
        "  - id: example\n"
        "    name: Example\n"
        "    runtime:\n"
        "      type: docker-compose\n"
        f"      project_directory: {outside}\n"
        "      compose_file: compose.yaml\n",
    )
    # Replace the allowed root with a distinct path after creating the helper fixture.
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    content = path.read_text(encoding="utf-8")
    content = content.replace(str(tmp_path / "fixture"), str(allowed), 1)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RegistryError, match="outside allowed roots"):
        ApplicationRegistry.load(path)


def test_application_id_command_injection_is_not_treated_as_an_id(tmp_path: Path) -> None:
    registry = ApplicationRegistry.load(write_registry(tmp_path, "[]\n"))
    with pytest.raises(ApplicationNotFoundError):
        registry.get("example; systemctl start ssh.service")
