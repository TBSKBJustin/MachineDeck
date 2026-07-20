from types import SimpleNamespace
from unittest.mock import patch

from app.orchestration.ports import addresses_conflict, endpoint_url
from app.schemas.applications import PortDefinition


def web_port(**overrides: object) -> PortDefinition:
    value = {
        "id": "web",
        "name": "Web UI",
        "protocol": "http",
        "host_port": 8188,
        "bind_address": "0.0.0.0",
        "path": "/ui?search=hello world&mode=full",
        "primary": True,
        "open_in_browser": True,
    }
    value.update(overrides)
    return PortDefinition.model_validate(value)


def test_wildcard_and_loopback_conflict_but_distinct_specific_addresses_do_not() -> None:
    assert addresses_conflict("0.0.0.0", "127.0.0.1")
    assert addresses_conflict("::", "127.0.0.1")
    assert not addresses_conflict("127.0.0.1", "192.168.1.50")


def test_wildcard_web_url_uses_trusted_local_host_and_encodes_query() -> None:
    url = endpoint_url(web_port())
    assert url == "http://127.0.0.1:8188/ui?search=hello+world&mode=full"


def test_ipv6_and_https_urls_are_formatted_safely() -> None:
    url = endpoint_url(
        web_port(protocol="https", bind_address="2001:db8::1", host_port=8443, path="/")
    )
    assert url == "https://[2001:db8::1]:8443/"


def test_non_web_port_never_returns_browser_url() -> None:
    port = PortDefinition.model_validate(
        {"id": "raw", "name": "Raw TCP", "protocol": "tcp", "host_port": 9000}
    )
    assert endpoint_url(port) is None


def test_malicious_configured_public_host_is_not_used() -> None:
    fake_settings = SimpleNamespace(
        public_host_local="evil.example@attacker.test", public_host_lan=None
    )
    with patch("app.orchestration.ports.settings", fake_settings):
        assert endpoint_url(web_port()) is None
