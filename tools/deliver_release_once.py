#!/usr/bin/env python3
"""Revision-pinned, at-most-once miniapp announcement in the serialized bot job.

Encrypted backup checkpoints precede every handoff. A handoff with an unknown
outcome is never repeated. No other process may use the account concurrently.
This never exports addresses, fingerprints or message contents to Actions logs.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile

import requests
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from michiflin import access, chat, config, miniapp, release_selection, state  # noqa: E402

RELEASE_ID = 'ux-update-20261119-v1'
# An independent, permanent single-use tag survives restoration of an older
# state snapshot. Never delete it or infer safe retry from an absent local log.
ONCE_TAG = 'michiflin-release-dispatch-ux-20261119-v1'
LEDGER = 'requested_release_deliveries'
STATES = frozenset({'planned', 'attempting', 'registered', 'delivered', 'pending', 'failed',
                    'skipped', 'skipped_current', 'uncertain', 'encryption_unconfirmed'})


class StopSafely(Exception):
    """Message is a fixed category, never an identity or exception detail."""


def _armed() -> str:
    revision = os.getenv('RELEASE_APP_SEND_REVISION', '').strip()
    if (os.getenv('CONFIRM_RELEASE_DELIVERY') != '1'
            or os.getenv('GITHUB_REPOSITORY') != 'YosvanyAlain/michiflin-ai'
            or os.getenv('GITHUB_REF') != 'refs/heads/main'
            or os.getenv('GITHUB_RUN_ATTEMPT', '1') != '1'
            or not re.fullmatch(r'[0-9a-f]{40}', revision) or revision != os.getenv('RUNNING_SHA', '').strip()
            or os.getenv('STATE_BACKUP_REQUIRED') != '1' or os.getenv('STATE_BACKUP_STORAGE') != 'releases'
            or os.getenv('STATE_BACKUP_NAMESPACE') != 'owner-clean-v1'
            or os.getenv('STATE_BACKUP_FENCE_REQUIRED') != '1' or os.getenv('STATE_BACKUP_RETAIN_ALL') != '1'
            or os.getenv('STATE_BACKUP_INCLUDE_BLOBS') != '1' or not config.OWNER_ADDRESS
            or not re.fullmatch(r'[0-9a-f]{48}', config.BOT_EPOCH)
            or not config.INVITE_AUTO_ACCESS or not config.BOT_VERIFIED_CONTACTS_ONLY or config.EMAIL_KEY_RECOVERY
            or not config.BOT_EPOCH_STRICT or not config.MINIAPP_CURRENT_ONLY or not config.REVOKE_LEGACY_MINIAPPS):
        raise StopSafely('not_armed_for_exact_revision')
    return revision


def _package() -> str:
    """Verify actual attachment bytes against every source asset and the CRC."""
    path = miniapp.build()
    expected = miniapp.app_package.entries(miniapp.APP_FILES)
    try:
        if not 0 < path.stat().st_size < 4_600_000:
            raise StopSafely('app_package_invalid')
        with zipfile.ZipFile(path) as archive:
            if archive.namelist() != [name for name, _ in expected] or archive.testzip() is not None:
                raise StopSafely('app_package_invalid')
            for name, source in expected:
                if hashlib.sha256(archive.read(name)).digest() != hashlib.sha256((miniapp.APP_DIR / source).read_bytes()).digest():
                    raise StopSafely('app_package_invalid')
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, zipfile.BadZipFile, KeyError):
        raise StopSafely('app_package_invalid') from None


def _digest(salt: str, purpose: str, *values: Any) -> str:
    payload = json.dumps([purpose, *values], ensure_ascii=True, separators=(',', ':')).encode('utf-8')
    return hmac.new(bytes.fromhex(salt), payload, hashlib.sha256).hexdigest()


def _key(salt: str, address: str) -> str:
    return _digest(salt, 'recipient', address)


def _binding(salt: str, item: release_selection.Candidate) -> str:
    return _digest(salt, 'binding', item.address, item.chat_id, item.contact_id, item.grant, item.fingerprint)


def _checkpoint(backup: Callable[[], None]) -> None:
    try:
        state.save()
        backup()  # encrypted upload, download/hash verification, then mark ready
    except Exception:
        raise StopSafely('backup_checkpoint_failed') from None


def _acquire_once() -> None:
    """Create a private, atomic single-use marker; ambiguous responses stop.

    Unlike the encrypted state ledger, the marker is outside any restorable
    snapshot. A 422, timeout or preexisting tag never grants permission to
    send, even when it points to the expected anchor.
    """
    import state_backup
    try:
        settings = state_backup.configuration()
        if not settings:
            raise StopSafely('once_marker_unavailable')
        token, repo, _password = settings
        state_backup._assert_private(token, repo)
        anchor = state_backup._get_ref(token, repo, state_backup.RELEASE_TAG)
        if (not anchor or state_backup._get_ref(token, repo, state_backup.CLEAN_OWNER_READY_TAG) != anchor
                or state_backup._release(token, repo) is None):
            raise StopSafely('once_marker_unavailable')
        if state_backup._get_ref(token, repo, ONCE_TAG) is not None:
            raise StopSafely('once_marker_already_present')
        response = requests.post(state_backup._git_api(repo) + '/refs', headers=state_backup.headers(token),
                                 json={'ref': 'refs/tags/' + ONCE_TAG, 'sha': anchor},
                                 timeout=30, allow_redirects=False)
        if response.status_code != 201:
            raise StopSafely('once_marker_not_acquired')
        body = response.json()
        if (not isinstance(body, dict) or body.get('ref') != 'refs/tags/' + ONCE_TAG
                or not isinstance(body.get('object'), dict) or body['object'].get('sha') != anchor):
            raise StopSafely('once_marker_not_confirmed')
        # GitHub's read replicas may briefly return 404 after a confirmed 201.
        # Retry only the readback, never the creation POST. A mismatch or an
        # unconfirmed marker still blocks all mail and any blind retry.
        for delay in (0, 1, 2, 4, 8, 8):
            if delay:
                time.sleep(delay)
            observed = state_backup._get_ref(token, repo, ONCE_TAG)
            if observed == anchor:
                break
            if observed is not None:
                raise StopSafely('once_marker_not_confirmed')
        else:
            raise StopSafely('once_marker_not_confirmed')
    except StopSafely:
        raise
    except Exception:
        raise StopSafely('once_marker_uncertain_do_not_retry') from None


def _require_empty_outbox(root: Path) -> None:
    """Do not start I/O in this auxiliary process with old mail in flight."""
    databases = list((root / 'accounts').rglob('dc.db'))
    if len(databases) != 1:
        raise StopSafely('outbox_state_unavailable')
    try:
        with sqlite3.connect(f'file:{databases[0]}?mode=ro', uri=True) as db:
            ok = db.execute('PRAGMA quick_check').fetchone()
            pending = db.execute('SELECT count(*) FROM msgs WHERE state IN (18,20) AND deleted=0').fetchone()
        if not ok or ok[0] != 'ok' or not pending or pending[0] != 0:
            raise StopSafely('outbox_not_empty')
    except StopSafely:
        raise
    except Exception:
        raise StopSafely('outbox_state_unavailable') from None


def _wait_idle_mail(rpc: Any, account: int, timeout: int = 90) -> None:
    """Wait for core connectivity; never overtake a known unprocessed inbox.

    Mail may still be in transit; a connected indicator cannot prove that no
    newer remote request exists. Normal live authorization is checked again at
    each handoff, and any locally visible pending mail stops this operation.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if int(rpc.get_connectivity(account)) >= 4000:
            time.sleep(2)
            break
        time.sleep(1)
    else:
        raise StopSafely('mail_connectivity_unconfirmed')
    _require_empty_inbox(rpc, account)


def _require_empty_inbox(rpc: Any, account: int) -> None:
    with state.lock:
        inbox = state.data.get('inbox', {})
        if not isinstance(inbox, dict) or inbox:
            raise StopSafely('unprocessed_inbox')
    for msg_id in rpc.get_next_msgs(account):
        message = rpc.get_message(account, msg_id)
        # The core cursor can also return this process's own outgoing messages
        # and informational events; neither is an unseen access instruction.
        if getattr(message, 'from_id', None) not in (0, 1, 2) and not getattr(message, 'is_info', False):
            raise StopSafely('unprocessed_inbox')


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    entries = record['recipients'].values()
    counts = {name: sum(item.get('state') == name for item in entries) for name in sorted(STATES)}
    return {'plan_complete': record['phase'] == 'complete', 'recipients': len(record['recipients']), **counts}


def _validated(record: Any, revision: str, build: str, package_hash: str) -> bool:
    if (not isinstance(record, dict) or record.get('version') != 1 or record.get('revision') != revision
            or record.get('epoch') != config.BOT_EPOCH or record.get('build') != build
            or record.get('package_sha256') != package_hash or record.get('phase') not in {'planned', 'complete'}
            or not re.fullmatch(r'[0-9a-f]{64}', str(record.get('salt') or ''))):
        return False
    items = record.get('recipients')
    return (isinstance(items, dict) and len(items) <= 10000 and all(
        re.fullmatch(r'[0-9a-f]{64}', str(key)) and isinstance(value, dict)
        and value.get('state') in STATES and re.fullmatch(r'[0-9a-f]{64}', str(value.get('binding') or ''))
        for key, value in items.items()))


def deliver(rpc: Any, account: int, backup: Callable[[], None], *, timeout: int = 20,
            acquire: Callable[[], None] | None = None, wait_mail: Callable[[Any, int], None] | None = None) -> dict[str, Any]:
    """Plan current eligible users, then hand off at most once per release.

    An independent one-shot tag prevents a retry even if an older backup is
    later restored. A crash can leave planned users unsent; never blindly retry.
    The injectable callables are for isolated tests, not deployment inputs.
    """
    revision = _armed()
    if config.MAINTENANCE_MODE or state.maintenance_active():
        raise StopSafely('maintenance_active')
    package_hash, build = _package(), miniapp.app_build()
    candidates = release_selection.select(rpc, account, build)
    if not any(item.address == config.OWNER_ADDRESS.casefold() for item in candidates):
        raise StopSafely('owner_identity_not_verified')
    with state.lock:
        ledger = state.data.setdefault(LEDGER, {})
        if not isinstance(ledger, dict):
            raise StopSafely('invalid_ledger')
        record = ledger.get(RELEASE_ID)
        if record is None:
            salt = secrets.token_hex(32)
            recipients = {_key(salt, item.address): {'binding': _binding(salt, item),
                          'state': 'skipped_current' if item.already_current else 'planned'} for item in candidates}
            if len(recipients) != len(candidates):
                raise StopSafely('duplicate_recipient')
            record = {'version': 1, 'revision': revision, 'epoch': config.BOT_EPOCH, 'build': build,
                      'package_sha256': package_hash, 'salt': salt, 'phase': 'planned', 'recipients': recipients}
            ledger[RELEASE_ID] = record
            new_plan = True
        else:
            new_plan = False
    if not _validated(record, revision, build, package_hash):
        raise StopSafely('invalid_prior_plan')
    if record['phase'] == 'complete':
        return _summary(record)
    if new_plan:
        _checkpoint(backup)  # an immutable plan before mail I/O starts
    salt = record['salt']
    by_key = {_key(salt, item.address): item for item in candidates}
    # A process could have crashed after an actual RPC handoff. Never re-send it.
    uncertain = False
    for item in record['recipients'].values():
        if item['state'] == 'attempting':
            item['state'] = 'uncertain'
            uncertain = True
    if uncertain:
        _checkpoint(backup)
    io_started = False
    try:
        pending = [(key, item) for key, item in record['recipients'].items() if item['state'] == 'planned']
        if pending:
            rpc.start_io(account)
            io_started = True
            (wait_mail or _wait_idle_mail)(rpc, account)
        # Exactly one process may ever claim this release, even after a stale
        # restore. If the POST outcome is ambiguous, no mail is handed off.
        (acquire or _acquire_once)()
        for key, item in pending:
            _require_empty_inbox(rpc, account)
            old = by_key.get(key)
            current = release_selection.candidate(rpc, account, old.address, build) if old else None
            if not current or not hmac.compare_digest(item['binding'], _binding(salt, current)):
                item['state'] = 'skipped'
                _checkpoint(backup)
                continue
            if current.already_current:
                item['state'] = 'skipped_current'
                _checkpoint(backup)
                continue
            # The 'attempting' checkpoint is durable BEFORE any handoff. If a
            # later return value is uncertain, we cannot accidentally repeat it.
            item['state'] = 'attempting'
            _checkpoint(backup)
            try:
                with state.guard_work(current.address), chat.recipient_guard(current.chat_id, current.address, current.contact_id):
                    def changed() -> bool:
                        try:
                            _require_empty_inbox(rpc, account)
                            fresh = release_selection.candidate(rpc, account, current.address, build)
                            return bool(not fresh or fresh.already_current or not hmac.compare_digest(item['binding'], _binding(salt, fresh)))
                        except Exception:
                            return True
                    msg_id = int(miniapp.send_app(current.chat_id, current.locale, address=current.address,
                                                  interrupted=changed, announce_release=True) or 0)
            except Exception:
                msg_id = 0
            if not msg_id:
                item['state'] = 'uncertain'
                _checkpoint(backup)
                continue
            item.update(state='registered', message_id=msg_id)
            _checkpoint(backup)  # includes core message and new app registration
            try:
                outcome = chat.wait_delivery(msg_id, timeout, threading.Event())
                message = rpc.get_message(account, msg_id)
                encrypted = getattr(message, 'show_padlock', False) is True
            except Exception:
                outcome, encrypted = 'pending', False
            if outcome == 'delivered' and encrypted:
                item['state'] = 'delivered'
            elif outcome == 'delivered':
                item['state'] = 'encryption_unconfirmed'
                _checkpoint(backup)
                raise StopSafely('delivered_encryption_unconfirmed')
            elif outcome == 'failed':
                item['state'] = 'failed'
            else:
                item['state'] = 'pending'
            # The next intent (or the final checkpoint) also persists this
            # observation. The preceding 'registered' snapshot remains safe.
        record['phase'] = 'complete'
        _checkpoint(backup)
    finally:
        if io_started:
            try:
                rpc.stop_io(account)
            except Exception:
                pass
    return _summary(record)


def main() -> int:
    try:
        from deltachat2 import IOTransport, Rpc
        from types import SimpleNamespace
        import state_backup
        _armed()
        root = Path(config.CONFIG_DIR)
        if not state.STATE_FILE.is_file() or not (root / 'accounts').is_dir() or not state_backup.configuration():
            raise StopSafely('account_or_backup_unavailable')
        logger = logging.getLogger('michiflin')
        logger.handlers[:] = [logging.NullHandler()]
        logger.propagate = False
        state.load()
        access.restore_policy()
        _require_empty_outbox(root)
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
            result = deliver(rpc, account, backup)
        print('SAFE_REQUESTED_RELEASE ' + json.dumps(result, sort_keys=True))
        return 0
    except StopSafely as exc:
        print('SAFE_REQUESTED_RELEASE_STOPPED', str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print('SAFE_REQUESTED_RELEASE_STOPPED', type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
