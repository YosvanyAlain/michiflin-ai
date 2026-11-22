"""Explicit owner-only recovery: synthetic identities, no live messages."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from michiflin import config, identity_guard, miniapp, owner_recovery, state

REV = 'a' * 40


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    import state_backup
    backup_save = state_backup.save
    monkeypatch.setattr(state_backup, 'save', Mock())
    monkeypatch.setenv('OWNER_APP_RECOVERY_REVISION', REV)
    monkeypatch.setenv('RUNNING_SHA', REV)
    for name, value in [('OWNER_ONLY', True), ('BOT_EPOCH_STRICT', True),
                        ('BOT_VERIFIED_CONTACTS_ONLY', True), ('EMAIL_KEY_RECOVERY', False),
                        ('OWNER_ADDRESS', 'owner@example.invalid'), ('BOT_EPOCH', 'synthetic-epoch'),
                        ('OWNER_PGP_FINGERPRINT', 'B' * 40), ('CONFIG_DIR', tmp_path)]:
        monkeypatch.setattr(config, name, value)
    marker = {'confirmed': True, 'epoch': config.BOT_EPOCH, 'owner_address': config.OWNER_ADDRESS,
              'owner_fingerprint': config.OWNER_PGP_FINGERPRINT, 'owner_chat': 42}
    marker_path = tmp_path / '.owner-clean-origin-v1.json'
    marker_path.write_text(json.dumps(marker))
    monkeypatch.setattr(owner_recovery.chat, 'ready', lambda: True)
    monkeypatch.setattr(owner_recovery.chat, 'rpc', lambda: SimpleNamespace())
    monkeypatch.setattr(owner_recovery.chat, 'account', lambda: 1)
    verify = Mock(return_value=True)
    monkeypatch.setattr(identity_guard, 'authorized_entry', verify)
    apps = {'100': {'chat': 42, 'addr': config.OWNER_ADDRESS, 'epoch': config.BOT_EPOCH, 'local_data': 'preserve'}}
    monkeypatch.setitem(state.data, 'apps', apps)
    monkeypatch.setitem(state.data, 'owner_refresh_intents', {})
    original_send = miniapp.send_app
    send = Mock(return_value=101)
    monkeypatch.setattr(miniapp, 'send_app', send)
    return SimpleNamespace(send=send, verify=verify, apps=apps, marker=marker, marker_path=marker_path,
                           original_send=original_send, backup_save=backup_save)


def test_requested_delivery_preserves_existing_apps_and_targets_only_owner(recovery):
    before = copy.deepcopy(recovery.apps)
    assert owner_recovery.deliver()
    recovery.send.assert_called_once_with(42, 'es', updated=True, address=config.OWNER_ADDRESS, recovery_revision=REV)
    assert recovery.apps == before


@pytest.mark.parametrize('wrong', ['b' * 40, '', 'not-a-commit'])
def test_other_revision_cannot_trigger_recovery(recovery, monkeypatch, wrong):
    monkeypatch.setenv('OWNER_APP_RECOVERY_REVISION', wrong)
    assert not owner_recovery.deliver()
    recovery.send.assert_not_called()


@pytest.mark.parametrize('field,value', [('owner_fingerprint', 'C' * 40), ('owner_address', 'guest@example.invalid'),
                                       ('epoch', 'other'), ('confirmed', False), ('owner_chat', 1)])
def test_mismatched_marker_never_sends(recovery, field, value):
    recovery.marker[field] = value
    recovery.marker_path.write_text(json.dumps(recovery.marker))
    assert not owner_recovery.deliver()
    recovery.send.assert_not_called()


def test_unverified_chat_never_receives_recovery(recovery):
    recovery.verify.return_value = False
    assert not owner_recovery.deliver()
    recovery.send.assert_not_called()


def test_persisted_revision_prevents_repeated_delivery_after_restart(recovery):
    recovery.apps['101'] = {'addr': config.OWNER_ADDRESS, 'epoch': config.BOT_EPOCH, 'recovery_revision': REV}
    assert not owner_recovery.deliver()
    recovery.send.assert_not_called()


def test_recovery_tag_is_checkpointed_with_normal_app_registration(recovery, monkeypatch):
    # Exercise the real sender method, but replace every transport operation.
    monkeypatch.setattr(miniapp, '_verified_binding', lambda _: True)
    monkeypatch.setattr(miniapp.chat, 'sender', lambda: 'owner@example.invalid')
    send = Mock(return_value=101)
    monkeypatch.setattr(miniapp.chat, 'send', send)
    monkeypatch.setattr(miniapp, 'build', lambda: 'synthetic.xdc')
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'synthetic-build')
    monkeypatch.setattr(miniapp.maintenance, 'text', lambda _: '')
    monkeypatch.setattr(miniapp.access, 'session_stamp', lambda _: '')
    monkeypatch.setitem(state.data, 'apps', {'100': {'preserved': True}})
    save = Mock()
    monkeypatch.setattr(state, 'save', save)
    assert recovery.original_send(42, 'es', updated=True, recovery_revision=REV) == 101
    assert state.data['apps']['101']['recovery_revision'] == REV
    assert state.data['apps']['100'] == {'preserved': True}
    save.assert_called_once()
    assert 'ESTE mensaje' in send.call_args.args[1]


def test_explicit_ui_refresh_uses_current_verified_owner_once(recovery, monkeypatch):
    monkeypatch.setenv('OWNER_REQUESTED_APP_REVISION', REV)
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: True)
    monkeypatch.setattr(state, 'save', Mock())
    monkeypatch.setattr(owner_recovery.chat, 'wait_delivery', Mock(return_value='delivered'))
    monkeypatch.setattr(config, 'OWNER_ONLY', False)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', True)
    monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', True)
    before = copy.deepcopy(recovery.apps)
    assert owner_recovery.deliver_requested_refresh()
    recovery.send.assert_called_once_with(42, 'es', updated=True, address=config.OWNER_ADDRESS, recovery_revision=REV)
    assert recovery.apps == before
    recovery.apps['101'] = {'addr':config.OWNER_ADDRESS, 'epoch':config.BOT_EPOCH, 'recovery_revision':REV}
    assert not owner_recovery.deliver_requested_refresh()
    assert recovery.send.call_count == 1


@pytest.mark.parametrize('reason', ['unarmed', 'other_revision', 'unverified', 'foreign_epoch'])
def test_explicit_ui_refresh_cannot_send_without_exact_request_and_binding(recovery, monkeypatch, reason):
    monkeypatch.setenv('OWNER_REQUESTED_APP_REVISION', REV)
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: reason != 'unverified')
    if reason == 'unarmed':
        monkeypatch.delenv('OWNER_REQUESTED_APP_REVISION')
    elif reason == 'other_revision':
        monkeypatch.setenv('RUNNING_SHA', 'b'*40)
    elif reason == 'foreign_epoch':
        recovery.apps['100']['epoch'] = 'other'
    assert not owner_recovery.deliver_requested_refresh()
    recovery.send.assert_not_called()


@pytest.mark.parametrize('failure', ['none', 'before_send', 'after_send'])
def test_owner_refresh_reserves_and_checkpoints_before_send_without_rotation(recovery, monkeypatch, failure):
    import state_backup
    monkeypatch.setenv('OWNER_REQUESTED_APP_REVISION', REV)
    monkeypatch.setenv('STATE_BACKUP_REQUIRED', '1')
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: True)
    monkeypatch.setattr(state, 'save', Mock())
    monkeypatch.setattr(owner_recovery.chat, 'wait_delivery', Mock(return_value='delivered'))
    snapshots = []
    def checkpoint(*, rotate):
        assert rotate is False
        snapshots.append(copy.deepcopy(state.data))
        if (failure == 'before_send' and len(snapshots) == 1) or (failure == 'after_send' and len(snapshots) == 2):
            raise RuntimeError('synthetic checkpoint failure')
    monkeypatch.setattr(state_backup, 'save', Mock(side_effect=checkpoint))
    def register(*args, **kwargs):
        assert snapshots and snapshots[0]['owner_refresh_intents'][REV]['state'] == 'reserved'
        recovery.apps['101'] = {'addr': config.OWNER_ADDRESS, 'epoch': config.BOT_EPOCH, 'recovery_revision': REV}
        return 101
    recovery.send.side_effect = register
    assert owner_recovery.deliver_requested_refresh() is (failure != 'before_send')
    assert len(snapshots) == (1 if failure == 'before_send' else 2)
    assert recovery.send.call_count == (0 if failure == 'before_send' else 1)
    assert not owner_recovery.deliver_requested_refresh()
    assert recovery.send.call_count == (0 if failure == 'before_send' else 1)
    assert recovery.apps['100']['local_data'] == 'preserve'


def test_uncertain_send_and_restored_reservation_are_not_retried(recovery, monkeypatch):
    import state_backup
    monkeypatch.setenv('OWNER_REQUESTED_APP_REVISION', REV)
    monkeypatch.setenv('STATE_BACKUP_REQUIRED', '1')
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: True)
    monkeypatch.setattr(state, 'save', Mock())
    snapshots = []
    monkeypatch.setattr(state_backup, 'save', lambda *, rotate: snapshots.append(copy.deepcopy(state.data)))
    recovery.send.side_effect = RuntimeError('delivery outcome unknown')
    assert not owner_recovery.deliver_requested_refresh()
    assert recovery.send.call_count == 1 and len(snapshots) == 1
    assert snapshots[0]['owner_refresh_intents'][REV]['state'] == 'reserved'
    # Simulate a fresh process loading the verified pre-send checkpoint.
    monkeypatch.setitem(state.data, 'owner_refresh_intents', copy.deepcopy(snapshots[0]['owner_refresh_intents']))
    recovery.send.side_effect = None
    assert not owner_recovery.deliver_requested_refresh()
    assert recovery.send.call_count == 1


def test_invalid_intent_ledger_fails_closed_without_send(recovery, monkeypatch):
    monkeypatch.setenv('OWNER_REQUESTED_APP_REVISION', REV)
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: True)
    monkeypatch.setitem(state.data, 'owner_refresh_intents', 'invalid-synthetic-ledger')
    assert not owner_recovery.deliver_requested_refresh()
    recovery.send.assert_not_called()


def test_concurrent_owner_refresh_requests_reserve_only_once(recovery, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv('OWNER_REQUESTED_APP_REVISION', REV)
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: True)
    monkeypatch.setattr(state, 'save', Mock())
    monkeypatch.setattr(owner_recovery.chat, 'wait_delivery', Mock(return_value='delivered'))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: owner_recovery.deliver_requested_refresh(), range(2)))
    assert sorted(results) == [False, True]
    recovery.send.assert_called_once()


def test_owner_refresh_does_not_send_if_local_reservation_cannot_be_saved(recovery, monkeypatch):
    import state_backup
    monkeypatch.setenv('OWNER_REQUESTED_APP_REVISION', REV)
    monkeypatch.setenv('STATE_BACKUP_REQUIRED', '1')
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: True)
    monkeypatch.setattr(state, 'save', Mock(side_effect=OSError('synthetic disk error')))
    checkpoint = Mock(); monkeypatch.setattr(state_backup, 'save', checkpoint)
    assert not owner_recovery.deliver_requested_refresh()
    recovery.send.assert_not_called()
    checkpoint.assert_not_called()


def test_real_encrypted_snapshot_contains_reservation_before_synthetic_handoff(recovery, monkeypatch, tmp_path):
    """Real archive, encryption, verification and restore; network and mail are simulated."""
    import sqlite3
    import state_backup

    synthetic = copy.deepcopy(state.data)
    monkeypatch.setattr(state, 'data', synthetic)
    recovery.apps = synthetic['apps']
    root = tmp_path / 'synthetic-config'
    root.mkdir()
    with sqlite3.connect(root / 'synthetic.sqlite') as db:
        db.execute('CREATE TABLE sample(value TEXT)')
        db.execute("INSERT INTO sample VALUES('fixture')")
    monkeypatch.setattr(state, 'CONFIG_DIR', root)
    monkeypatch.setattr(state, 'STATE_FILE', root / 'michiflin-state.json')
    monkeypatch.setenv('CONFIG_DIR', str(root))
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-token')
    monkeypatch.setenv('STATE_BACKUP_REPO', 'owner/private')
    monkeypatch.setenv('STATE_BACKUP_PASSPHRASE', 'synthetic-passphrase')
    monkeypatch.setenv('STATE_BACKUP_REQUIRED', '1')
    monkeypatch.setenv('STATE_BACKUP_STORAGE', 'releases')
    monkeypatch.setenv('OWNER_REQUESTED_APP_REVISION', REV)
    monkeypatch.delenv('STATE_BACKUP_NAMESPACE', raising=False)
    monkeypatch.setattr(miniapp, '_verified_binding', lambda _: True)
    monkeypatch.setattr(owner_recovery.chat, 'wait_delivery', Mock(return_value='delivered'))
    monkeypatch.setattr(state_backup, 'save', recovery.backup_save)
    monkeypatch.setattr(state_backup, '_assert_private', Mock())
    monkeypatch.setattr(state_backup, '_ensure_release', Mock(return_value={'id': 7}))
    monkeypatch.setattr(state_backup, '_mark_ready', Mock())
    prune = Mock(); monkeypatch.setattr(state_backup, '_prune', prune)
    uploads = []
    def upload(url, **kwargs):
        assert url == 'https://uploads.github.com/repos/owner/private/releases/7/assets'
        uploads.append(kwargs['data'])
        return Mock(json=lambda: {'id': len(uploads)})
    def rename(url, **kwargs):
        assert url == f'https://api.github.com/repos/owner/private/releases/assets/{len(uploads)}'
        return Mock(json=lambda: {'id': len(uploads), 'name': kwargs['json']['name'], 'state': 'uploaded'})
    monkeypatch.setattr(state_backup.requests, 'post', Mock(side_effect=upload))
    monkeypatch.setattr(state_backup.requests, 'patch', Mock(side_effect=rename))
    monkeypatch.setattr(state_backup, '_download_asset', lambda *args: uploads[-1])

    def read_snapshot(blob, output):
        assert b'owner_refresh_intents' not in blob
        plaintext = state_backup.decrypt_backup(blob, 'synthetic-passphrase')
        state_backup.safely_extract(plaintext, output)
        return json.loads((output / 'michiflin/michiflin-state.json').read_text())

    def send(*_args, **_kwargs):
        assert len(uploads) == 1
        restored = read_snapshot(uploads[0], tmp_path / 'before-send')
        assert restored['owner_refresh_intents'][REV] == {'state': 'reserved', 'epoch': config.BOT_EPOCH}
        assert '101' not in restored['apps']
        recovery.apps['101'] = {'addr': config.OWNER_ADDRESS, 'epoch': config.BOT_EPOCH, 'recovery_revision': REV}
        return 101
    recovery.send.side_effect = send
    assert owner_recovery.deliver_requested_refresh()
    assert len(uploads) == 2
    restored = read_snapshot(uploads[1], tmp_path / 'after-send')
    assert restored['owner_refresh_intents'][REV]['state'] == 'reserved'
    assert restored['apps']['101']['recovery_revision'] == REV
    assert restored['apps']['100']['local_data'] == 'preserve'
    prune.assert_not_called()
