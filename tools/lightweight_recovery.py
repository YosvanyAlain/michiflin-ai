"""Owner-authorized offline light recovery of an already verified account.

This does not create an identity, start mail IO, delete remote messages, or
replace backups. Invoke only on the isolated restored directory before launch.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from michiflin.media_retention import account_blob, is_downloaded_media


def hold_old_work(data: dict, now: float) -> int:
    count = 0
    for job in data.get('jobs', {}).values():
        if isinstance(job, dict) and job.get('status') not in {'done', 'failed', 'cancelled'}:
            job.update(hold=True, error='delivery_unknown' if job.get('inflight') else 'recovery_review', updated=now)
            count += 1
    # The old durable inbox is not evidence that its requests went unanswered
    # during the later, uncheckpointed session. Do not replay them automatically.
    skipped = [str(key) for key in data.get('inbox', {}) if str(key).isdigit()]
    data['inbox'] = {}
    data.setdefault('flags', {})['lightweight_recovery'] = {
        'version': 1, 'at': now, 'older_queue_held': count, 'skip_message_ids': skipped}
    return count


def discard_media_files(root: Path) -> tuple[int, int]:
    count = size = 0
    for path in (root / 'accounts').rglob('*'):
        if is_downloaded_media(path.name) and account_blob(path, root):
            size += path.stat().st_size
            path.unlink()
            count += 1
    return count, size


def prepare(root: Path) -> dict:
    from deltachat2 import IOTransport, Rpc
    import state_backup
    root = root.resolve()
    state_file = root / 'michiflin-state.json'
    if not state_file.is_file() or not (root / state_backup.CLEAN_OWNER_ORIGIN).is_file():
        raise ValueError('verified clean-owner state is required')
    origin = json.loads((root / state_backup.CLEAN_OWNER_ORIGIN).read_text())
    if not state_backup._clean_origin_valid(origin):
        raise ValueError('clean-owner provenance is invalid')
    data = json.loads(state_file.read_text())
    if not isinstance(data, dict) or not isinstance(data.get('jobs', {}), dict):
        raise ValueError('state is invalid')
    now = time.time()
    # Starting the JSON-RPC subprocess alone does not call start_io/configure.
    # Delete only stale local outbox entries, never remote/recipient history.
    pending_removed = 0
    with IOTransport(accounts_dir=str(root / 'accounts')) as transport:
        rpc = Rpc(transport)
        accounts = rpc.get_all_account_ids()
        if len(accounts) != 1 or not rpc.is_configured(accounts[0]):
            raise ValueError('exactly one configured restored account is required')
        account = accounts[0]
        stale = []
        for chat_id in rpc.get_chatlist_entries(account, None, None, None):
            for msg_id in rpc.get_message_ids(account, chat_id, False, False):
                message = rpc.get_message(account, msg_id)
                view = str(getattr(message.view_type, 'value', message.view_type))
                if int(message.state) in {18, 20} and view != 'Webxdc':
                    stale.append(int(msg_id))
        if stale:
            rpc.delete_messages(account, stale)
            pending_removed = len(stale)
    held = hold_old_work(data, now)
    temporary = state_file.with_suffix('.recovery.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(state_file)
    files, removed_bytes = discard_media_files(root)
    return {'mail_io_started': False, 'new_identity_created': False, 'recipient_messages_deleted': False,
            'local_stale_outbox_removed': pending_removed, 'old_jobs_held': held,
            'local_media_files_removed': files, 'local_media_bytes_removed': removed_bytes,
            'identity_databases_preserved': True, 'settings_notes_apps_preserved': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config-dir', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = prepare(arguments.config_dir)
        arguments.report.write_text(json.dumps(result, indent=2))
        print('SAFE_LIGHT_RECOVERY ' + json.dumps(result))
    except Exception as exc:
        print('SAFE_LIGHT_RECOVERY_FAILED ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
