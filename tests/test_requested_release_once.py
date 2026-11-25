"""Synthetic tests for the owner's explicitly requested one-time release."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from michiflin import access, admin, chat, config, identity_guard, miniapp, release_selection, state
from tools import deliver_release_once as tool

REVISION = 'a' * 40
OWNER = 'owner@example.invalid'
GUEST = 'guest@example.invalid'


def entry(address, chat_id, build='old', sent=1, **kwargs):
    return {'addr': address, 'chat': chat_id, 'build': build, 'epoch': 'epoch',
            'access_session': 'valid', 'locale': 'en', 'sent': sent, **kwargs}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(config, 'BOT_EPOCH', 'epoch')
    monkeypatch.setattr(config, 'OWNER_ADDRESS', OWNER)
    monkeypatch.setattr(config, 'OWNER_BACKUP_ADDRESS', '')
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', True)
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', True)
    monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', False)
    monkeypatch.setattr(config, 'MINIAPP_CURRENT_ONLY', True)
    monkeypatch.setattr(config, 'MAINTENANCE_MODE', False)
    monkeypatch.setattr(state, 'data', {'apps': {}, 'chat_interfaces': {}})
    monkeypatch.setattr(state, 'save', Mock())
    monkeypatch.setattr(state, 'get_kill_switch', lambda addr: False)
    monkeypatch.setattr(state, 'maintenance_active', lambda: False)
    monkeypatch.setattr(access, 'bound_allowed', lambda e: e.get('access_session') == 'valid')
    monkeypatch.setattr(access, 'session_stamp', lambda addr: '' if addr == OWNER else 'valid')
    monkeypatch.setattr(admin, 'is_allowed', lambda addr: addr in {OWNER, GUEST})
    monkeypatch.setattr(identity_guard, 'authorized_chat', lambda rpc, account, cid, addr, **kw: cid in {21, 22})
    for key, value in {'CONFIRM_RELEASE_DELIVERY': '1', 'GITHUB_REPOSITORY': 'YosvanyAlain/michiflin-ai',
                       'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ATTEMPT': '1',
                       'RUNNING_SHA': REVISION, 'RELEASE_APP_SEND_REVISION': REVISION,
                       'STATE_BACKUP_REQUIRED': '1'}.items():
        monkeypatch.setenv(key, value)
    rpc = Mock()
    rpc.lookup_contact_id_by_addr.side_effect = lambda account, addr: {OWNER: 1, GUEST: 2}.get(addr, 0)
    rpc.get_chat_id_by_contact_id.side_effect = lambda account, contact: {1: 21, 2: 22}.get(contact, 0)
    rpc.get_message.return_value = SimpleNamespace(show_padlock=True)
    return rpc


def test_selects_distinct_current_grants_and_latest_binding(setup):
    state.data['apps'] = {'1': entry(OWNER, 21, sent=1), '2': entry(OWNER, 21, sent=10),
                          '3': entry(GUEST, 22, sent=2), '4': entry('stranger@example.invalid', 23),
                          '5': entry(GUEST, 22, sent=3, access_session='revoked')}
    selected = release_selection.select(setup, 1, 'new')
    assert [(x['addr'], x['sent']) for x in selected] == [(GUEST, 2), (OWNER, 10)]


def test_current_build_suppresses_delivery_even_if_historical_apps_exist(setup):
    state.data['apps'] = {'1': entry(OWNER, 21), '2': entry(OWNER, 21, build='new', sent=2),
                          '3': entry(GUEST, 22)}
    assert [x['addr'] for x in release_selection.select(setup, 1, 'new')] == [GUEST]


def test_chat_only_wrong_chat_key_change_revoked_or_paused_are_excluded(setup, monkeypatch):
    state.data['apps'] = {'1': entry(OWNER, 21), '2': entry(GUEST, 22)}
    state.data['chat_interfaces'][f'{GUEST}:22'] = {'chat': 22, 'epoch': 'epoch', 'grant': 'valid', 'mode': 'full'}
    assert [x['addr'] for x in release_selection.select(setup, 1, 'new')] == [OWNER]
    state.data['chat_interfaces'][f'{GUEST}:22']['grant'] = 'stale'
    monkeypatch.setattr(identity_guard, 'authorized_chat', lambda rpc, account, cid, addr, **kw: cid != 21)
    assert [x['addr'] for x in release_selection.select(setup, 1, 'new')] == [GUEST]
    setup.get_chat_id_by_contact_id.return_value = 999
    setup.get_chat_id_by_contact_id.side_effect = None
    assert not release_selection.select(setup, 1, 'new')
    monkeypatch.setattr(state, 'maintenance_active', lambda: True)
    assert not release_selection.select(setup, 1, 'new')


@pytest.mark.parametrize('flag', ['INVITE_AUTO_ACCESS', 'BOT_VERIFIED_CONTACTS_ONLY', 'MINIAPP_CURRENT_ONLY'])
def test_unsafe_modes_fail_closed(setup, monkeypatch, flag):
    state.data['apps'] = {'1': entry(OWNER, 21)}
    monkeypatch.setattr(config, flag, False)
    assert not release_selection.select(setup, 1, 'new')


def setup_delivery(monkeypatch, rpc, targets):
    monkeypatch.setattr(tool, '_package', lambda: (Path('/tmp/synthetic.xdc'), 'a' * 64))
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'new')
    monkeypatch.setattr(release_selection, 'select', lambda *args: copy.deepcopy(targets))
    monkeypatch.setattr(release_selection, 'eligible', lambda *args: True)
    sent = []
    def send(chat_id, locale, **kwargs):
        assert kwargs['release_caption'] is True
        sent.append((chat_id, locale))
        state.data['apps'][str(300 + len(sent))] = {'chat': chat_id, 'build': 'new'}
        return 300 + len(sent)
    monkeypatch.setattr(miniapp, 'send_app', send)
    monkeypatch.setattr(chat, 'wait_delivery', lambda *args: 'delivered')
    snapshots = []
    def backup():
        snapshots.append(copy.deepcopy(state.data))
    return sent, snapshots, backup


def test_plan_is_checkpointed_before_mail_and_each_binding_before_next_send(setup, monkeypatch):
    targets = [entry(OWNER, 21), entry(GUEST, 22)]
    sent, snapshots, backup = setup_delivery(monkeypatch, setup, targets)
    result = tool.deliver(setup, 1, backup)
    assert result == {'previous_plan': False, 'planned': 2, 'delivered': 2}
    assert len(sent) == 2 and setup.start_io.call_count == setup.stop_io.call_count == 1
    first = snapshots[0][tool.LEDGER][tool.RELEASE_ID]
    assert first['phase'] == 'reserved' and all(x['state'] == 'reserved' for x in first['recipients'].values())
    assert not snapshots[0]['apps']
    assert '301' in snapshots[1]['apps'] and '302' not in snapshots[1]['apps']
    assert '302' in snapshots[2]['apps']
    assert snapshots[-1][tool.LEDGER][tool.RELEASE_ID]['phase'] == 'complete'
    assert tool.deliver(setup, 1, backup) == {'previous_plan': True}
    assert len(sent) == 2


def test_failed_initial_checkpoint_never_connects_or_sends(setup, monkeypatch):
    sent, _, _ = setup_delivery(monkeypatch, setup, [entry(OWNER, 21)])
    def broken():
        raise OSError('private error')
    with pytest.raises(tool.StopSafely, match='backup_checkpoint_failed'):
        tool.deliver(setup, 1, broken)
    setup.start_io.assert_not_called()
    assert not sent and tool.deliver(setup, 1, broken) == {'previous_plan': True}


def test_failed_post_handoff_checkpoint_never_starts_second_recipient(setup, monkeypatch):
    sent, _, backup = setup_delivery(monkeypatch, setup, [entry(OWNER, 21), entry(GUEST, 22)])
    calls = 0
    def broken():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('private error')
        backup()
    with pytest.raises(tool.StopSafely, match='backup_checkpoint_failed'):
        tool.deliver(setup, 1, broken)
    assert len(sent) == 1
    setup.stop_io.assert_called_once()
    assert tool.deliver(setup, 1, broken) == {'previous_plan': True}
    assert len(sent) == 1


def test_ambiguous_handoff_is_held_and_never_retried(setup, monkeypatch):
    _, snapshots, backup = setup_delivery(monkeypatch, setup, [entry(OWNER, 21)])
    sender = Mock(return_value=0)
    monkeypatch.setattr(miniapp, 'send_app', sender)
    tool.deliver(setup, 1, backup)
    assert [x['state'] for x in snapshots[-1][tool.LEDGER][tool.RELEASE_ID]['recipients'].values()] == ['uncertain']
    tool.deliver(setup, 1, backup)
    sender.assert_called_once()


def test_delivery_is_not_claimed_when_padlock_is_missing(setup, monkeypatch):
    _, snapshots, backup = setup_delivery(monkeypatch, setup, [entry(OWNER, 21)])
    setup.get_message.return_value = SimpleNamespace(show_padlock=False)
    result = tool.deliver(setup, 1, backup)
    assert result['delivered'] == 0
    assert [x['state'] for x in snapshots[-1][tool.LEDGER][tool.RELEASE_ID]['recipients'].values()] == ['pending']


@pytest.mark.parametrize('name,value', [('RELEASE_APP_SEND_REVISION', 'b' * 40),
                                        ('GITHUB_REF', 'refs/heads/other'),
                                        ('GITHUB_RUN_ATTEMPT', '2'),
                                        ('CONFIRM_RELEASE_DELIVERY', '0')])
def test_one_shot_must_match_exact_revision_and_first_attempt(setup, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(tool.StopSafely, match='not_armed_for_exact_revision'):
        tool.deliver(setup, 1, lambda: None)
    setup.start_io.assert_not_called()


@pytest.mark.parametrize('locale,fragment', [('es', 'no se trasladan'), ('en', 'does not transfer')])
def test_caption_is_short_and_explains_no_migration(setup, monkeypatch, locale, fragment):
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: True)
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'new')
    monkeypatch.setattr(miniapp, 'build', lambda: Path('/tmp/synthetic.xdc'))
    sender = Mock(return_value=101)
    monkeypatch.setattr(chat, 'send', sender)
    miniapp.send_app(21, locale, address=OWNER, release_caption=True)
    assert fragment in sender.call_args.args[1] and len(sender.call_args.args[1]) < 100
    assert sender.call_args.kwargs['viewtype'] == 'Webxdc'
