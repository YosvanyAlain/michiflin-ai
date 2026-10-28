"""Disposable, public diagnostic of the real Live sandbox through WARP.

This test-only branch instruments the worker to return only a boolean from the
public Cloudflare trace page. It does not log the IP, screenshot or page body.
"""
from __future__ import annotations

import json
import select
from pathlib import Path
from unittest import mock

from michiflin import sandbox

TRACE = "https://www.cloudflare.com/cdn-cgi/trace"
original_copy = sandbox.shutil.copyfile


def instrument(src, dst, *args, **kwargs):
    if Path(src).name != "remote_worker.py":
        return original_copy(src, dst, *args, **kwargs)
    text = Path(src).read_text()
    marker = "            emit(frame(page, detail))\n"
    if text.count(marker) != 1:
        raise RuntimeError("browser diagnostic marker changed")
    replacement = (
        "            snapshot = frame(page, detail)\n"
        "            snapshot['warp_on'] = any(line.strip() == 'warp=on' for line in "
        "page.locator('body').inner_text(timeout=10000).splitlines())\n"
        "            emit(snapshot)\n"
    )
    Path(dst).write_text(text.replace(marker, replacement))
    return dst


def main() -> None:
    with mock.patch.object(sandbox.shutil, "copyfile", side_effect=instrument), sandbox.BrowserProcess() as worker:
        if worker.route != "proxy" or worker.process is None:
            raise RuntimeError("Live did not configure the proxy route")
        proc = worker.process
        if not proc.stdin or not proc.stdout:
            raise RuntimeError("Live worker pipes unavailable")
        payload = {"url": TRACE, "storage": None, "detail": "economy"}
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        proc.stdin.flush()
        if not select.select([proc.stdout], [], [], 60)[0]:
            raise RuntimeError("Live trace timed out")
        raw = proc.stdout.readline(3_000_001)
        result = json.loads(raw)
        kind_ok = result.get("kind") == "frame"
        warp_ok = result.get("warp_on") is True
        proxy_ok = worker.gateway is not None and not worker.gateway.take_proxy_failure()
        print("Live route: proxy")
        print("Live frame:", "ok" if kind_ok else "failed")
        print("Live trace:", "warp=on" if warp_ok else "warp not confirmed")
        print("Gateway proxy connection:", "ok" if proxy_ok else "failed")
        if not (kind_ok and warp_ok and proxy_ok):
            raise RuntimeError("Live/WARP verification failed")


if __name__ == "__main__":
    main()
