"""The requested update can only use the existing, authenticated owner's binding."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import deltachat2
from michiflin import config, miniapp, state, chat
from tools import deliver_owner_refresh as tool


@pytest.fixture
def fixture(monkeypatch):
    rev = 'a' * 40
    monkeypatch.setenv('RUNNING_SHA', rev)
    monkeypatch.setenv('OWNER_UI_REFRESH_REVISION', rev)
    monkeypatch.setattr(config, 'OWNER_ONLY', True)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', False)
    monkeypatch.setattr(config, 'OWNER_ADDRESS', 'owner@example.invalid')
    monkeypatch.setattr(config, 'BOT_EPOCH', 'synthetic')
    monkeypatch.setattr(state, 'load', Mock())
    monkeypatch.setattr(state, 'save', Mock())
    entries = {'1': {'addr': 'other@example.invalid', 'chat': 99, 'epoch': 'synthetic', 'sent': 20},
               '2': {'addr': config.OWNER_ADDRESS, 'chat': 42, 'epoch': 'synthetic', 'sent': 10}}
    monkeypatch.setattr(miniapp, '_apps', lambda: entries)
    verify = Mock(return_value=True)
    monkeypatch.setattr(miniapp, '_verified_binding', verify)
    send = Mock(return_value=101)
    monkeypatch.setattr(miniapp, 'send_app', send)
    monkeypatch.setattr(miniapp, 'app_build', lambda: '0123456789')
    rpc = Mock()
    rpc.get_all_account_ids.return_value = [1]
    rpc.is_configured.return_value = True
    transport = Mock()
    transport.__enter__ = Mock(return_value=transport)
    transport.__exit__ = Mock(return_value=False)
    io = Mock(return_value=transport)
    monkeypatch.setattr(deltachat2, 'IOTransport', io)
    monkeypatch.setattr(deltachat2, 'Rpc', lambda transport: rpc)
    monkeypatch.setattr(chat, 'bind', Mock())
    monkeypatch.setattr(chat, 'wait_delivery', Mock(return_value='delivered'))
    return SimpleNamespace(entries=entries, verify=verify, rpc=rpc, io=io, send=send, rev=rev)


def test_update_targets_only_current_owner_without_new_identity(fixture):
    report = tool.deliver()
    assert report['delivery'] == 'delivered' and report['previous_instances_preserved']
    fixture.send.assert_called_once_with(42, 'es', updated=True,
                                        address='owner@example.invalid', recovery_revision=fixture.rev)
    assert fixture.verify.call_args.args[0]['addr'] == 'owner@example.invalid'
    fixture.rpc.start_io.assert_called_once_with(1)
    fixture.rpc.stop_io.assert_called_once_with(1)
    fixture.rpc.add_account.assert_not_called()
    fixture.rpc.configure.assert_not_called()


def test_already_registered_revision_does_not_send_again(fixture):
    fixture.entries['2']['recovery_revision'] = fixture.rev
    assert tool.deliver()['delivery'] == 'previously_registered'
    fixture.send.assert_not_called()
    fixture.io.assert_not_called()


def test_unverified_owner_never_starts_io_or_sends(fixture):
    fixture.verify.return_value = False
    with pytest.raises(ValueError):
        tool.deliver()
    fixture.rpc.start_io.assert_not_called()
    fixture.send.assert_not_called()


@pytest.mark.parametrize('policy', ['revision', 'owner_only', 'invite_access'])
def test_wrong_revision_or_shared_access_never_starts_io(fixture, monkeypatch, policy):
    if policy == 'revision':
        monkeypatch.setenv('OWNER_UI_REFRESH_REVISION', 'b' * 40)
    elif policy == 'owner_only':
        monkeypatch.setattr(config, 'OWNER_ONLY', False)
    else:
        monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', True)
    with pytest.raises(ValueError):
        tool.deliver()
    fixture.io.assert_not_called()
    fixture.send.assert_not_called()


def test_open_private_chats_release_still_delivers_only_to_existing_owner(fixture, monkeypatch):
    monkeypatch.setattr(config, 'OWNER_ONLY', False)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', True)
    monkeypatch.setattr(config, 'REVOKE_LEGACY_MINIAPPS', True)
    report = tool.deliver()
    assert report['delivery'] == 'delivered'
    assert fixture.send.call_args.args == (42, 'es')
    assert fixture.send.call_args.kwargs['address'] == 'owner@example.invalid'
    fixture.rpc.add_account.assert_not_called()
