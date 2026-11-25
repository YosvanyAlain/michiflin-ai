#!/usr/bin/env python3
"""One explicitly armed, revision-pinned release to existing private-chat users.

Runs only in the serialized bot workflow before the serving process opens the
account. Current grants and verified encrypted individual chats are rechecked.
A durable encrypted reservation precedes all mail I/O; uncertain attempts are
never retried. Old miniapps, their messages and local data remain untouched.
No addresses, message IDs, keys, provider responses or content reach the logs.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import zipfile
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from michiflin import access, chat, config, miniapp, release_selection, state  # noqa: E402

RELEASE_ID = 'integrated-private-update-20261119-v1'
LEDGER = 'requested_release_deliveries'


class StopSafely(Exception):
    """Reason is a fixed code, never an identity or an exception payload."""


def _armed() -> str:
    requested = os.getenv('RELEASE_APP_SEND_REVISION', '').strip()
    running = os.getenv('RUNNING_SHA', '').strip()
    if (os.getenv('CONFIRM_RELEASE_DELIVERY') != '1'
            or os.getenv('GITHUB_REPOSITORY') != 'YosvanyAlain/michiflin-ai'
            or os.getenv('GITHUB_REF') != 'refs/heads/main'
            or os.getenv('GITHUB_RUN_ATTEMPT', '1') != '1'
            or not re.fullmatch(r'[0-9a-f]{40}', requested) or requested != running
            or os.getenv('STATE_BACKUP_REQUIRED') != '1'
            or not config.INVITE_AUTO_ACCESS or not config.BOT_VERIFIED_CONTACTS_ONLY
            or config.EMAIL_KEY_RECOVERY or not config.MINIAPP_CURRENT_ONLY):
        raise StopSafely('not_armed_for_exact_revision')
    return requested


def _package() -> tuple[Path, str]:
    path = miniapp.build()
    expected = miniapp.app_package.entries(miniapp.APP_FILES)
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.namelist() != [name for name, _ in expected] or archive.testzip() is not None:
                raise StopSafely('app_package_mismatch')
            for name, source in expected:
                if hashlib.sha256(archive.read(name)).digest() != hashlib.sha256((miniapp.APP_DIR / source).read_bytes()).digest():
                    raise StopSafely('app_package_mismatch')
        return path, hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, zipfile.BadZipFile, KeyError):
        raise StopSafely('app_package_mismatch') from None


def _target_key(address: str) -> str:
    return hashlib.sha256((RELEASE_ID + '\0' + address.casefold()).encode('utf-8')).hexdigest()


def _checkpoint(backup: Callable[[], None]) -> None:
    state.save()
    try:
        backup()
    except Exception:
        raise StopSafely('backup_checkpoint_failed') from None


def deliver(rpc: Any, account: int, backup: Callable[[], None], *, timeout: int = 120) -> dict[str, Any]:
    """Exactly one plan, at-most-once handoffs; backup before and after each.

    Return values are fixed aggregate counts. If an earlier attempt exists,
    including a merely reserved/uncertain one, never restart or extend it.
    """
    revision = _armed()
    if config.MAINTENANCE_MODE or state.maintenance_active():
        raise StopSafely('maintenance_active')
    with state.lock:
        ledger = state.data.setdefault(LEDGER, {})
        if not isinstance(ledger, dict):
            raise StopSafely('invalid_ledger')
        if RELEASE_ID in ledger:
            return {'previous_plan': True}
    _path, package_hash = _package()
    current_build = miniapp.app_build()
    targets = release_selection.select(rpc, account, current_build)
    items = {_target_key(str(e['addr']).strip().casefold()): {'state': 'reserved'} for e in targets}
    if len(items) != len(targets):
        raise StopSafely('duplicate_target_key')
    with state.lock:
        # A competing plan must never replace a reserved plan.
        if RELEASE_ID in ledger:
            return {'previous_plan': True}
        record = {'version': 1, 'revision': revision, 'build': current_build,
                  'package_sha256': package_hash, 'phase': 'reserved', 'recipients': items}
        ledger[RELEASE_ID] = record
    # A verified encrypted checkpoint of the entire plan is required before
    # connecting to mail or handing off any message.
    _checkpoint(backup)
    if not targets:
        record['phase'] = 'complete'
        _checkpoint(backup)
        return {'previous_plan': False, 'planned': 0, 'delivered': 0}
    io_started = False
    try:
        rpc.start_io(account)
        io_started = True
        for entry in targets:
            address = str(entry['addr']).strip().casefold()
            item = items[_target_key(address)]
            if not release_selection.eligible(rpc, account, entry):
                item['state'] = 'skipped'
                _checkpoint(backup)
                continue
            locale = entry.get('locale') if entry.get('locale') in {'es', 'en'} else 'es'
            try:
                msg_id = int(miniapp.send_app(int(entry['chat']), locale, address=address, release_caption=True) or 0)
            except Exception:
                msg_id = 0
            if not msg_id:
                # RPC handoff might have happened even if no ID came back.
                item['state'] = 'uncertain'
                _checkpoint(backup)
                continue
            item.update(state='registered', message_id=msg_id)
            # Preserve the new binding and core message immediately, before
            # waiting on network delivery; a failed checkpoint stops the batch.
            _checkpoint(backup)
            try:
                outcome = chat.wait_delivery(msg_id, timeout, threading.Event())
                message = rpc.get_message(account, msg_id)
                encrypted = getattr(message, 'show_padlock', False) is True
            except Exception:
                outcome, encrypted = 'pending', False
            if outcome == 'delivered' and encrypted:
                item['state'] = 'delivered'
            elif outcome == 'failed':
                item['state'] = 'failed'
            else:
                item['state'] = 'pending'
            # The following recipient's checkpoint, or the final checkpoint,
            # also persists this outcome. A restored 'registered' state is
            # uncertain and must never be retried blindly.
            state.save()
        record['phase'] = 'complete'
        _checkpoint(backup)
    finally:
        if io_started:
            try:
                rpc.stop_io(account)
            except Exception:
                pass
    return {'previous_plan': False, 'planned': len(targets),
            'delivered': sum(x.get('state') == 'delivered' for x in items.values())}


def main() -> int:
    try:
        from deltachat2 import IOTransport, Rpc
        from types import SimpleNamespace
        import state_backup

        _armed()
        root = Path(config.CONFIG_DIR)
        if not root.is_dir() or not (root / 'accounts').is_dir() or not state_backup.configuration():
            raise StopSafely('account_or_backup_unavailable')
        # Suppress library exception messages in public Actions logs; the
        # fixed safe result below is the only output of this one-shot step.
        logger = logging.getLogger('michiflin')
        logger.handlers[:] = [logging.NullHandler()]
        logger.propagate = False
        state.load()
        access.restore_policy()
        with IOTransport(accounts_dir=str(root / 'accounts'), stderr=subprocess.DEVNULL) as transport:
            rpc = Rpc(transport)
            accounts = rpc.get_all_account_ids()
            if len(accounts) != 1 or not rpc.is_configured(accounts[0]):
                raise StopSafely('account_unavailable')
            account = int(accounts[0])
            chat.bind(SimpleNamespace(rpc=rpc), account)
            def backup() -> None:
                with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    state_backup.save(rotate=False)
            deliver(rpc, account, backup)
        print('SAFE_REQUESTED_RELEASE_PLAN_RECORDED')
        return 0
    except StopSafely as exc:
        print('SAFE_REQUESTED_RELEASE_STOPPED', str(exc))
        return 1
    except Exception as exc:
        print('SAFE_REQUESTED_RELEASE_STOPPED', type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
