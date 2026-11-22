"""Remove only the retired text feature's live files after verified backup.

Workflow-only, separately and explicitly armed for an exact revision. An app
refresh alone must never remove files. Never touches a backup, account, other
SQLite store or running session. The workflow must run this after its mandatory
encrypted backup and recovery verification, before starting the bot.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat


def retire(root: Path) -> int:
    revision = os.getenv('UI_RETIRE_STORE_REVISION', '')
    if (not re.fullmatch(r'[0-9a-f]{40}', revision)
            or revision != os.getenv('RUNNING_SHA', '')
            or os.getenv('UI_RETIRE_BACKUP_VERIFIED') != '1'):
        return 0
    paths = [root / ('text-social.sqlite3' + suffix) for suffix in ('', '-wal', '-shm', '-journal')]
    # Validate the entire set before removing any entry. Never follow a link.
    existing = []
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe_retired_store')
        existing.append(path)
    for path in existing:
        path.unlink()
    return len(existing)


if __name__ == '__main__':
    try:
        print('Retired UI live files removed after verified backup:', retire(Path(os.environ['CONFIG_DIR'])))
    except Exception as exc:
        print('Retired UI cleanup stopped:', type(exc).__name__)
        raise SystemExit(1)
