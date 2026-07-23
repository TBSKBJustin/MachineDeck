from __future__ import annotations

import ipaddress
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings
from app.security.auth import client_is_trusted_network, request_network_context


def request(
    peer: str,
    headers: dict[str, str] | None = None,
    *,
    scheme: str = "http",
) -> Request:
    encoded = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": scheme,
            "path": "/api/v1/auth/login",
            "raw_path": b"/api/v1/auth/login",
            "query_string": b"",
            "headers": encoded,
            "client": (peer, 12345),
            "server": ("127.0.0.1", 8080),
        }
    )


def network_settings(**changes: object) -> object:
    defaults = {
        "trusted_proxies": (
            ipaddress.ip_network("127.0.0.1/32"),
            ipaddress.ip_network("10.0.0.0/24"),
        ),
        "trusted_networks": (
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("192.168.1.0/24"),
        ),
        "forwarded_hop_limit": 8,
    }
    return replace(settings, **{**defaults, **changes})


def test_untrusted_peer_cannot_spoof_forwarding_headers() -> None:
    incoming = request(
        "192.168.1.20",
        {
            "x-forwarded-for": "127.0.0.1",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "attacker.example",
        },
    )
    with patch("app.security.auth.settings", network_settings()):
        context = request_network_context(incoming)
    assert context.peer == context.client == "192.168.1.20"
    assert context.scheme == "http"
    assert context.forwarded is False


def test_trusted_proxy_uses_nearest_untrusted_hop_from_bounded_chain() -> None:
    incoming = request(
        "127.0.0.1",
        {
            "x-forwarded-for": "203.0.113.9, 10.0.0.4",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "machine.example.ts.net",
        },
    )
    with patch("app.security.auth.settings", network_settings()):
        context = request_network_context(incoming)
    assert context.peer == "127.0.0.1"
    assert context.client == "203.0.113.9"
    assert context.scheme == "https"
    assert context.host == "machine.example.ts.net"
    assert context.forwarded is True


def test_standard_forwarded_header_is_supported_only_from_trusted_peer() -> None:
    incoming = request(
        "::1",
        {
            "forwarded": (
                'for="[2001:db8::25]:443";proto=https;host="machine.example", '
                "for=10.0.0.4"
            )
        },
    )
    trusted = network_settings(
        trusted_proxies=(
            ipaddress.ip_network("::1/128"),
            ipaddress.ip_network("10.0.0.0/24"),
        )
    )
    with patch("app.security.auth.settings", trusted):
        context = request_network_context(incoming)
    assert context.client == "2001:db8::25"
    assert context.scheme == "https"
    assert context.host == "machine.example"


@pytest.mark.parametrize(
    "headers",
    [
        {"x-forwarded-for": ", ".join(["10.0.0.1"] * 9)},
        {
            "forwarded": "for=203.0.113.9",
            "x-forwarded-for": "203.0.113.9",
        },
        {"x-forwarded-for": "not-an-address"},
        {
            "x-forwarded-for": "203.0.113.9",
            "x-forwarded-proto": "https,http",
        },
    ],
)
def test_malformed_trusted_proxy_headers_fail_closed(headers: dict[str, str]) -> None:
    with (
        patch("app.security.auth.settings", network_settings()),
        pytest.raises(HTTPException) as raised,
    ):
        request_network_context(request("127.0.0.1", headers))
    assert raised.value.status_code == 400
    assert raised.value.detail["code"] == "FORWARDED_HEADERS_INVALID"


def test_trusted_network_is_policy_metadata_not_authentication() -> None:
    with patch("app.security.auth.settings", network_settings()):
        context = request_network_context(request("192.168.1.55"))
        assert client_is_trusted_network(context)
    assert context.forwarded is False

