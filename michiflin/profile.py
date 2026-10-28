"""Maintain the bot's public Delta Chat status without replacing a custom description."""
from __future__ import annotations

import subprocess
from contextlib import contextmanager
from typing import Any, Iterator

REPOSITORY_URL = "https://github.com/YosvanyAlain/michiflin-ai"
DEFAULT_DESCRIPTION = "Private email assistant for web, downloads and AI."
START_HINT = "Envía /app para abrir la miniapp · Send /app to open the mini app."


def profile_status(current: str | None) -> str:
    """Keep existing text, and append the repository and start instructions when missing."""
    status = (current or "").strip() or DEFAULT_DESCRIPTION
    if "/app" not in status.casefold():
        status += "\n" + START_HINT
    if REPOSITORY_URL.casefold() not in status.casefold():
        status += "\n" + REPOSITORY_URL
    return status


def ensure_profile(rpc: Any, account_id: int) -> bool:
    """Update the selected account only when necessary; propagate RPC failures to the caller."""
    current = rpc.get_config(account_id, "selfstatus")
    updated = profile_status(current)
    if updated == current:
        return False
    rpc.set_config(account_id, "selfstatus", updated)
    return True


@contextmanager
def _account_rpc(accounts_dir: str) -> Iterator[Any]:
    """Close and reap the short-lived Delta Chat RPC server, even after errors."""
    from deltachat2 import IOTransport, Rpc

    transport = IOTransport(accounts_dir=accounts_dir)
    try:
        with transport:
            yield Rpc(transport)
    finally:
        process = getattr(transport, "process", None)
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()


def configure_directory(config_dir: str) -> None:
    """Update and re-read the first account before serving, without printing its contents."""
    from pathlib import Path

    with _account_rpc(str(Path(config_dir) / "accounts")) as rpc:
        accounts = rpc.get_all_account_ids()
        if not accounts:
            raise RuntimeError("No Delta Chat account is available")
        account_id = accounts[0]  # matches the bot's default on_start account
        ensure_profile(rpc, account_id)
        current = rpc.get_config(account_id, "selfstatus")
        if not current or profile_status(current) != current:
            raise RuntimeError("Profile status could not be verified")


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m michiflin.profile CONFIG_DIR")
    configure_directory(sys.argv[1])
    print("Bot profile status verified")
