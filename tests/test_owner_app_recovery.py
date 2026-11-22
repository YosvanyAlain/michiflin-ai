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
    original_send = miniapp.send_app
    send = Mock(return_value=101)
    monkeypatch.setattr(miniapp, 'send_app', send)
    return SimpleNamespace(send=send, verify=verify, apps=apps, marker=marker, marker_path=marker_path, original_send=original_send)


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
