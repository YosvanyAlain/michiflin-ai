"""Release-only policy: no retired miniapp traffic, independent verified visitors."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from michiflin import access, admin, community, config, live_web, miniapp, state
from tools import open_private_invite_access as opening
from tests.test_invite_community import Rpc, ALICE, OWNER, FINGERPRINT


@pytest.fixture
def release(monkeypatch):
    original = copy.deepcopy(state.data)
    access.reset()
    for key, value in {
        'OWNER_ADDRESS': OWNER, 'OWNER_BACKUP_ADDRESS': '', 'OWNER_ONLY': False,
        'INVITE_AUTO_ACCESS': True, 'REVOKE_LEGACY_MINIAPPS': True,
        'BOT_EPOCH': 'same-account-epoch', 'MAINTENANCE_MODE': False,
        'MINIAPP_RELEASE': opening.MINIAPP_RELEASE,
    }.items():
        monkeypatch.setattr(config, key, value)
    state.data['apps'] = {
        '100': {'chat': 21, 'addr': OWNER, 'epoch': config.BOT_EPOCH, 'build': 'old', 'serial': 7},
        '101': {'chat': 21, 'addr': OWNER, 'epoch': config.BOT_EPOCH, 'build': 'current', 'serial': 0},
    }
    state.data['invitees'] = {}
    state.data['invite_policy'] = {}
    state.data.pop('public_access_release', None)
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'current')
    monkeypatch.setattr(miniapp, '_answers', {})
    monkeypatch.setattr(state, 'save', Mock())
    monkeypatch.setattr(state, 'load', Mock(return_value=False))
    monkeypatch.setattr(state, 'STATE_FILE', Mock())
    state.STATE_FILE.is_file.return_value = True
    monkeypatch.setattr(community, '_checkpoint', Mock())
    monkeypatch.setattr(miniapp, '_verified_binding', Mock(return_value=True))
    monkeypatch.setattr(access, 'bound_allowed', Mock(return_value=True))
    monkeypatch.setattr(admin, 'is_allowed', Mock(return_value=True))
    monkeypatch.setattr(config, 'service_access_allowed', Mock(return_value=True))
    monkeypatch.setattr(miniapp.chat, 'send_update', Mock(return_value=True))
    monkeypatch.setenv('PUBLIC_ACCESS_RELEASE', opening.RELEASE)
    monkeypatch.setenv('STATE_BACKUP_REQUIRED', '1')
    community._pending.clear()
    yield state.data['apps']
    state.data.clear(); state.data.update(original)
    community._pending.clear()
    access.reset()


def test_retired_bindings_preserved_but_never_authorized(release):
    assert not miniapp.compatible_build(release['100'])
    assert miniapp._instance(100) is None
    assert miniapp._instance(101) is release['101']
    assert miniapp.compatible_build(release['101'])
    assert not miniapp.discloses_infobars(release['100'])
    assert miniapp.instance_ids() == {100, 101}  # protect old local data/messages from pruning
    for value in ({}, {'build': 'current'}, {**release['101'], 'epoch': 'foreign'}):
        assert not miniapp.compatible_build(value)


@pytest.mark.parametrize('operation', sorted(miniapp.OPS))
def test_retired_miniapp_cannot_execute_even_recovery_or_owner_controls(release, monkeypatch, operation):
    run = Mock()
    response = Mock()
    monkeypatch.setitem(miniapp.OPS, operation, run)
    monkeypatch.setattr(miniapp, 'respond', response)
    miniapp.handle(100, release['100'], {'id': 'synthetic', 'op': operation, 'args': {}, 'lang': 'es'})
    run.assert_not_called()
    response.assert_not_called()


def test_no_old_transport_read_ack_replay_push_or_reply(release, monkeypatch):
    rpc = Mock()
    monkeypatch.setattr(miniapp.chat, 'rpc', lambda: rpc)
    monkeypatch.setattr(miniapp, '_admit', Mock())
    miniapp.on_status_update(100)
    rpc.get_webxdc_status_updates.assert_not_called()
    miniapp._route(100, release['100'], {'id': 'pending', 'op': 'hello'})
    miniapp._admit.assert_not_called()
    miniapp._ack(100, 'pending', entry=release['100'])
    miniapp.respond(100, 'pending', {'private': 'never delivered'})
    assert miniapp._answers == {}
    assert not miniapp._send_status(100, {'t': 'push'})
    miniapp.chat.send_update.assert_not_called()


def test_current_registered_binding_cannot_be_forged_by_old_client(release):
    forged = {**release['100'], 'build': 'current'}
    assert not miniapp._release_allows(100, forged)
    assert not miniapp._release_allows(9999, release['101'])
    assert not miniapp._release_allows(101, {**release['101'], 'chat': 22})
    assert miniapp._release_allows(101, dict(release['101']))


def test_forwarded_current_file_fails_original_message_chat_binding(release, monkeypatch):
    rpc = Mock()
    rpc.get_message.return_value = SimpleNamespace(chat_id=999, from_id=1)
    monkeypatch.setattr(miniapp.chat, 'rpc', lambda: rpc)
    monkeypatch.setattr(miniapp.chat, 'account', lambda: 1)
    assert live_web._identity(101) is None
    assert live_web._authorized(100) is None
    assert live_web._authorized(9999) is None


def test_revocation_applies_to_delayed_handoff(release):
    assert miniapp._send_status(101, {'t': 'ack'})
    guard = miniapp.chat.send_update.call_args.kwargs['interrupted']
    assert not guard()
    release['101']['build'] = 'retired-after-admission'
    assert guard()
    assert not miniapp._release_allows(101, {**release['101'], 'build': 'current'})


def test_retired_registration_blocks_automatic_welcome_without_restoring_access(release, monkeypatch):
    release.pop('101')
    monkeypatch.setattr(miniapp.chat, 'sender', lambda: OWNER)
    assert miniapp._registered_app_for_sender(21)
    assert not miniapp._bound_apps(21)
    send = Mock()
    monkeypatch.setattr(miniapp, 'send_app', send)
    monkeypatch.setattr(miniapp.maintenance, 'silent_active', lambda: False)
    miniapp.welcome(21, 'es')
    send.assert_not_called()
    assert '100' in release and miniapp._instance(100) is None


def test_public_opening_checkpoint_once_then_preserves_emergency_close(release):
    state.data['invite_policy'] = {'closed': True, 'until': 0}
    assert opening.apply()
    assert state.data['invite_policy']['closed'] is False
    community._checkpoint.assert_called_once()
    state.data['invite_policy']['closed'] = True
    assert not opening.apply()
    assert state.data['invite_policy']['closed'] is True
    assert community._checkpoint.call_count == 1


def test_public_opening_checkpoint_failure_restores_previous_policy(release):
    previous = {'closed': True, 'until': 0}
    state.data['invite_policy'] = copy.deepcopy(previous)
    community._checkpoint.side_effect = RuntimeError('synthetic backup failure')
    with pytest.raises(RuntimeError):
        opening.apply()
    assert state.data['invite_policy'] == previous
    assert 'public_access_release' not in state.data


@pytest.mark.parametrize('field,value', [('OWNER_ONLY', True), ('INVITE_AUTO_ACCESS', False),
    ('REVOKE_LEGACY_MINIAPPS', False), ('MINIAPP_RELEASE', 'wrong-generation'), ('MAINTENANCE_MODE', True), ('OWNER_ADDRESS', '')])
def test_public_opening_requires_exact_explicit_configuration(release, monkeypatch, field, value):
    monkeypatch.setattr(config, field, value)
    with pytest.raises(ValueError): opening.apply()
    state.load.assert_not_called()


def test_link_visitor_auto_admission_has_separate_key_bound_private_grant(release, monkeypatch):
    from michiflin import identity_guard
    rpc = Rpc(ALICE)
    monkeypatch.setattr(identity_guard, 'contact_fingerprint', lambda *args: FINGERPRINT)
    assert not access.service_allowed(ALICE)
    assert community.admit_from_securejoin(rpc, 1, 15)
    assert access.service_allowed(ALICE)
    assert not admin.is_admin(ALICE)
    assert not access.service_allowed('another@example.invalid')
    assert community.matches(ALICE, 21, FINGERPRINT)
    assert not community.matches(ALICE, 22, FINGERPRINT)
    assert not community.matches(ALICE, 21, 'B' * 40)


def test_launch_enables_private_link_access_and_revokes_all_legacy_packages():
    text = Path('.github/workflows/michiflin.yml').read_text()
    assert 'OWNER_ONLY: "0"' in text
    assert 'INVITE_AUTO_ACCESS: "1"' in text
    assert 'REVOKE_LEGACY_MINIAPPS: "1"' in text
    assert 'MINIAPP_RELEASE: "public-private-20261119-v1"' in text
    assert 'tools/open_private_invite_access.py' in text
    assert text.index('Verify backup recovery') < text.index('tools/open_private_invite_access.py')
    assert text.index('tools/open_private_invite_access.py') < text.index('Private chats and restricted')


def test_missing_state_never_opens_access_or_saves_defaults(release):
    state.STATE_FILE.is_file.return_value = False
    with pytest.raises(ValueError, match='existing state'):
        opening.apply()
    state.save.assert_not_called()
    community._checkpoint.assert_not_called()


def test_package_generation_changes_without_rotating_identity_or_job_epoch(monkeypatch):
    epoch = config.BOT_EPOCH
    monkeypatch.setattr(config, 'MINIAPP_RELEASE', '')
    previous = miniapp.app_build()
    monkeypatch.setattr(config, 'MINIAPP_RELEASE', 'synthetic-new-release')
    assert miniapp.app_build() != previous
    assert config.BOT_EPOCH == epoch


@pytest.mark.parametrize('variable', ['PUBLIC_ACCESS_RELEASE', 'STATE_BACKUP_REQUIRED'])
def test_public_opening_requires_exact_release_and_encrypted_checkpoint(release, monkeypatch, variable):
    monkeypatch.delenv(variable, raising=False)
    with pytest.raises(ValueError): opening.apply()
    state.load.assert_not_called()


def test_current_private_miniapp_still_executes_and_receives_response(release, monkeypatch):
    operation = Mock(return_value={'accepted': True})
    response = Mock()
    monkeypatch.setitem(miniapp.OPS, 'health', operation)
    monkeypatch.setattr(miniapp, 'respond', response)
    monkeypatch.setattr(miniapp.perf, 'note_op', Mock())
    miniapp.handle(101, dict(release['101']), {'id': 'fresh', 'op': 'health', 'args': {}, 'lang': 'es'})
    operation.assert_called_once_with(21, {}, 'es')
    response.assert_called_once_with(101, 'fresh', {'accepted': True}, '')


def test_revoked_worker_never_starts_operation_or_replies(release, monkeypatch):
    from concurrent.futures import Future
    operation = Mock()
    response = Mock()
    class DelayedExecutor:
        def submit(self, work):
            release['101']['build'] = 'retired-in-queue'
            future = Future()
            try: future.set_result(work())
            except Exception as exc: future.set_exception(exc)
            return future
    monkeypatch.setattr(miniapp, '_workers', DelayedExecutor())
    monkeypatch.setitem(miniapp.OPS, 'health', operation)
    monkeypatch.setattr(miniapp, 'respond', response)
    monkeypatch.setattr(miniapp.perf, 'note_op', Mock())
    miniapp.handle(101, dict(release['101']), {'id': 'queued', 'op': 'health', 'args': {}, 'lang': 'es'})
    operation.assert_not_called()
    response.assert_not_called()
