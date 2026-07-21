#!/usr/bin/env python3
"""End-to-end host acceptance for the authenticated live Dashboard."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets
from websockets.exceptions import ConnectionClosed


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"


def unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def wait_for_health(client: httpx.AsyncClient) -> None:
    for _ in range(60):
        try:
            response = await client.get("/health")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("Dashboard acceptance server did not become healthy")


async def validate() -> int:
    port = unused_port()
    origin = f"http://127.0.0.1:{port}"
    temporary = tempfile.TemporaryDirectory(prefix="machinedeck-dashboard-")
    environment = {
        **os.environ,
        "MACHINEDECK_DATABASE_URL": f"sqlite:///{Path(temporary.name) / 'dashboard.db'}",
        "MACHINEDECK_BIND_PORT": str(port),
        "MACHINEDECK_TRUSTED_ORIGINS": origin,
    }
    server = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output: dict[str, object] = {}
    exit_code = 1
    try:
        async with httpx.AsyncClient(base_url=origin, timeout=10) as client:
            await wait_for_health(client)
            page = await client.get("/")
            setup = await client.post(
                "/api/v1/auth/setup",
                headers={"Origin": origin},
                json={
                    "username": "acceptance-admin",
                    "password": "temporary acceptance password",
                },
            )
            setup.raise_for_status()
            token = setup.cookies.get("machinedeck_session")
            csrf = setup.json()["csrf_token"]
            authenticated_headers = {
                "Cookie": f"machinedeck_session={token}",
                "Origin": origin,
            }
            dashboard = await client.get(
                "/api/v1/dashboard", headers=authenticated_headers
            )
            dashboard.raise_for_status()
            snapshot = dashboard.json()
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/ws/v1/dashboard",
                origin=origin,
                additional_headers={"Cookie": f"machinedeck_session={token}"},
                open_timeout=10,
            ) as websocket:
                first_message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                logout = await client.post(
                    "/api/v1/auth/logout",
                    headers={
                        **authenticated_headers,
                        "X-CSRF-Token": csrf,
                    },
                )
                logout.raise_for_status()
                close_code = None
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        await asyncio.wait_for(
                            websocket.recv(), timeout=deadline - time.monotonic()
                        )
                    except ConnectionClosed as exc:
                        close_code = exc.code
                        break
            output = {
                "frontend": {
                    "status": page.status_code,
                    "security_headers": {
                        "content_security_policy": "content-security-policy" in page.headers,
                        "frame_options": page.headers.get("x-frame-options"),
                    },
                },
                "rest": {
                    "freshness": snapshot["freshness"],
                    "collection_duration_ms": snapshot["collection_duration_ms"],
                    "gpu_count": len(snapshot["gpus"]),
                    "collectors": snapshot["collectors"],
                },
                "websocket": {
                    "first_type": first_message.get("type"),
                    "close_after_logout": close_code,
                },
            }
            exit_code = 0 if (
                page.status_code == 200
                and first_message.get("type") == "dashboard_snapshot"
                and close_code == 4401
            ) else 1
    finally:
        if server.returncode is None:
            server.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(server.wait(), timeout=10)
            except asyncio.TimeoutError:
                server.kill()
                await server.wait()
        if exit_code and server.stdout:
            output["server_output"] = (await server.stdout.read()).decode(
                errors="replace"
            )[-4000:]
        temporary.cleanup()
        print(json.dumps(output, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(validate()))
