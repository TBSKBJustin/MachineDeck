#!/usr/bin/env python3
"""Small host-acceptance HTTP server managed by a MachineDeck user unit."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP method name
        body = b"machinedeck-port-fixture\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("port", type=int)
    arguments = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", arguments.port), Handler).serve_forever()
