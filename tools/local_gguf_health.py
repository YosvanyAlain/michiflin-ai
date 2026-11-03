#!/usr/bin/env python3
"""Check only the isolated local server; never log or print its API key."""
from __future__ import annotations

import http.client
import json
import os
import socket
from pathlib import Path


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 3.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def request(path: str, key: str = "") -> tuple[int, bytes]:
    conn = UnixConnection("/run/michiflin-local-gguf/llama.sock")
    try:
        headers = {"Accept": "application/json", "Connection": "close"}
        if key:
            headers["Authorization"] = "Bearer " + key
        conn.request("GET", path, headers=headers)
        result = conn.getresponse()
        return result.status, result.read(1_000_001)
    finally:
        conn.close()


def main() -> int:
    key_path = Path(os.environ["RUNNER_TEMP"]) / "michiflin-local-gguf-key"
    with key_path.open("r", encoding="ascii") as handle:
        key = handle.read(257).strip()
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        return 1
    try:
        status, body = request("/health")
        if status != 200 or json.loads(body).get("status") != "ok":
            return 1
        unauth, _ = request("/v1/models")
        auth, models = request("/v1/models", key)
        # Only the health endpoint is public. Model inference needs the key.
        if unauth != 401 or auth != 200 or not isinstance(json.loads(models), dict):
            return 1
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
