from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.applications import ApplicationManifest, validate_manifest_paths


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "compose"


def manifest_with_ports(ports: list[dict]) -> dict:
    return {
        "id": "port-app",
        "name": "Port App",
        "runtime": {"type": "compose", "working_dir": str(FIXTURE)},
        "ports": ports,
    }


def port(**overrides: object) -> dict:
    value = {
        "id": "web",
        "name": "Web UI",
        "protocol": "http",
        "host_port": 8188,
        "bind_address": "0.0.0.0",
        "path": "/",
        "primary": True,
        "open_in_browser": True,
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("host_port", [0, -1, 65536])
def test_port_range_is_enforced(host_port: int) -> None:
    with pytest.raises(ValidationError):
        ApplicationManifest.model_validate(manifest_with_ports([port(host_port=host_port)]))


def test_duplicate_port_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="port ids must be unique"):
        ApplicationManifest.model_validate(
            manifest_with_ports([port(), port(id="web", host_port=8288, primary=False)])
        )


def test_duplicate_host_ports_are_rejected() -> None:
    with pytest.raises(ValidationError, match="host ports must be unique"):
        ApplicationManifest.model_validate(
            manifest_with_ports([port(), port(id="metrics", primary=False)])
        )


def test_only_one_primary_web_endpoint_is_allowed() -> None:
    with pytest.raises(ValidationError, match="only one primary"):
        ApplicationManifest.model_validate(
            manifest_with_ports([port(), port(id="admin", host_port=8288)])
        )


@pytest.mark.parametrize("path", ["relative", "javascript:alert(1)", "//evil.example/path"])
def test_web_path_cannot_be_relative_or_contain_a_host(path: str) -> None:
    with pytest.raises(ValidationError):
        ApplicationManifest.model_validate(manifest_with_ports([port(path=path)]))


def test_tcp_port_cannot_define_web_fields() -> None:
    with pytest.raises(ValidationError, match="cannot define Web"):
        ApplicationManifest.model_validate(
            manifest_with_ports(
                [
                    port(
                        protocol="tcp",
                        path="/",
                        primary=False,
                        open_in_browser=False,
                    )
                ]
            )
        )


@pytest.mark.parametrize("address", ["example.com", "http://127.0.0.1", "127.0.0.1;evil"])
def test_bind_address_accepts_only_literal_ip_addresses(address: str) -> None:
    with pytest.raises(ValidationError, match="literal IPv4 or IPv6"):
        ApplicationManifest.model_validate(
            manifest_with_ports([port(bind_address=address)])
        )


def test_privileged_ports_require_explicit_policy() -> None:
    manifest = ApplicationManifest.model_validate(manifest_with_ports([port(host_port=443)]))
    result = validate_manifest_paths(manifest)
    assert not result.valid
    assert any(issue.code == "PRIVILEGED_PORT_NOT_ALLOWED" for issue in result.errors)


def test_legacy_host_field_is_read_but_serializes_as_host_port() -> None:
    value = port()
    value.pop("host_port")
    value["host"] = 8188
    manifest = ApplicationManifest.model_validate(manifest_with_ports([value]))
    dumped = manifest.model_dump(mode="json")
    assert dumped["ports"][0]["host_port"] == 8188
    assert "host" not in dumped["ports"][0]
