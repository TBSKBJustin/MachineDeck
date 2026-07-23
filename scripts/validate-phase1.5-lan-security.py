#!/usr/bin/env python3
"""Adversarial host acceptance for an installed MachineDeck LAN endpoint."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from websockets.exceptions import ConnectionClosed


ATTACKER_ORIGIN = "http://untrusted-origin.invalid"


def parse_origin(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError("base URL must be an HTTP(S) Origin")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("base URL contains an invalid port") from exc
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def detail_code(response: httpx.Response) -> str | None:
    try:
        detail = response.json().get("detail", {})
    except (ValueError, AttributeError):
        return None
    return detail.get("code") if isinstance(detail, dict) else None


async def rejected_websocket(
    url: str,
    *,
    origin: str,
    cookie: str,
) -> tuple[bool, str]:
    try:
        async with websockets.connect(
            url,
            origin=origin,
            additional_headers={"Cookie": cookie},
            open_timeout=10,
        ) as websocket:
            try:
                await asyncio.wait_for(websocket.recv(), timeout=3)
            except ConnectionClosed as exc:
                return exc.code == 4403, f"WebSocket close {exc.code}"
            return False, "untrusted WebSocket Origin was accepted"
    except Exception as exc:  # Actual pre-accept rejection varies by server version.
        status = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        return status == 403, f"HTTP {status}" if status else type(exc).__name__


async def accepted_websocket(
    url: str,
    *,
    origin: str,
    cookie: str,
) -> tuple[bool, str]:
    try:
        async with websockets.connect(
            url,
            origin=origin,
            additional_headers={"Cookie": cookie},
            open_timeout=10,
        ) as websocket:
            message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
            return (
                message.get("type") == "dashboard_snapshot",
                str(message.get("type")),
            )
    except Exception as exc:
        return False, type(exc).__name__


async def validate(base_url: str, username: str, password: str) -> int:
    scheme = "wss" if base_url.startswith("https://") else "ws"
    parsed = urlsplit(base_url)
    websocket_url = urlunsplit(
        (scheme, parsed.netloc, "/ws/v1/dashboard", "", "")
    )
    results: dict[str, dict[str, object]] = {}
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        health = await client.get("/health")
        results["health"] = {
            "passed": health.status_code == 200,
            "status": health.status_code,
        }

        anonymous = await client.get("/api/v1/applications")
        results["trusted_network_does_not_bypass_auth"] = {
            "passed": anonymous.status_code == 401,
            "status": anonymous.status_code,
            "code": detail_code(anonymous),
        }

        spoofed_x_forwarded = await client.get(
            "/api/v1/applications",
            headers={
                "X-Forwarded-For": "127.0.0.1",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "localhost:8080",
            },
        )
        results["spoofed_x_forwarded_does_not_bypass_auth"] = {
            "passed": spoofed_x_forwarded.status_code == 401,
            "status": spoofed_x_forwarded.status_code,
            "code": detail_code(spoofed_x_forwarded),
        }

        spoofed_forwarded = await client.get(
            "/api/v1/applications",
            headers={
                "Forwarded": (
                    'for="127.0.0.1";proto=https;host="localhost:8080"'
                ),
            },
        )
        results["spoofed_forwarded_does_not_bypass_auth"] = {
            "passed": spoofed_forwarded.status_code == 401,
            "status": spoofed_forwarded.status_code,
            "code": detail_code(spoofed_forwarded),
        }

        unknown_login_origin = await client.post(
            "/api/v1/auth/login",
            headers={
                "Origin": ATTACKER_ORIGIN,
                "X-Forwarded-For": "127.0.0.1",
            },
            json={"username": username, "password": "not-used-by-origin-check"},
        )
        results["unknown_login_origin_rejected"] = {
            "passed": (
                unknown_login_origin.status_code == 403
                and detail_code(unknown_login_origin) == "ORIGIN_NOT_ALLOWED"
            ),
            "status": unknown_login_origin.status_code,
            "code": detail_code(unknown_login_origin),
        }

        login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": base_url},
            json={"username": username, "password": password},
        )
        results["trusted_origin_login"] = {
            "passed": login.status_code == 200,
            "status": login.status_code,
            "code": detail_code(login),
        }
        if login.status_code != 200:
            print(json.dumps(results, indent=2))
            return 1
        csrf = login.json()["csrf_token"]
        session_token = client.cookies.get("machinedeck_session")
        if not session_token:
            results["trusted_origin_login"]["passed"] = False
            results["trusted_origin_login"]["error"] = "session Cookie missing"
            print(json.dumps(results, indent=2))
            return 1
        cookie = f"machinedeck_session={session_token}"

        unknown_post_origin = await client.post(
            "/api/v1/applications/validate",
            headers={
                "Origin": ATTACKER_ORIGIN,
                "X-CSRF-Token": csrf,
            },
            json={},
        )
        results["unknown_authenticated_post_origin_rejected"] = {
            "passed": (
                unknown_post_origin.status_code == 403
                and detail_code(unknown_post_origin) == "ORIGIN_NOT_ALLOWED"
            ),
            "status": unknown_post_origin.status_code,
            "code": detail_code(unknown_post_origin),
        }

        missing_csrf = await client.post(
            "/api/v1/applications/validate",
            headers={"Origin": base_url},
            json={},
        )
        results["trusted_network_does_not_bypass_csrf"] = {
            "passed": (
                missing_csrf.status_code == 403
                and detail_code(missing_csrf) == "CSRF_INVALID"
            ),
            "status": missing_csrf.status_code,
            "code": detail_code(missing_csrf),
        }

        rejected, rejected_detail = await rejected_websocket(
            websocket_url,
            origin=ATTACKER_ORIGIN,
            cookie=cookie,
        )
        results["unknown_websocket_origin_rejected"] = {
            "passed": rejected,
            "detail": rejected_detail,
        }
        accepted, accepted_detail = await accepted_websocket(
            websocket_url,
            origin=base_url,
            cookie=cookie,
        )
        results["trusted_websocket_origin_accepted"] = {
            "passed": accepted,
            "detail": accepted_detail,
        }

        logout = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": base_url, "X-CSRF-Token": csrf},
        )
        results["logout"] = {
            "passed": logout.status_code == 204,
            "status": logout.status_code,
        }

    passed = all(bool(item.get("passed")) for item in results.values())
    print(json.dumps(results, indent=2))
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate an installed LAN endpoint. Run from another LAN device "
            "for the strongest direct-peer acceptance evidence."
        )
    )
    parser.add_argument(
        "--base-url",
        required=True,
        type=parse_origin,
        help="Exact trusted MachineDeck Origin, for example http://192.168.1.50:8080",
    )
    parser.add_argument("--username", required=True)
    arguments = parser.parse_args()
    password = getpass.getpass("MachineDeck administrator password: ")
    return asyncio.run(validate(arguments.base_url, arguments.username, password))


if __name__ == "__main__":
    raise SystemExit(main())
