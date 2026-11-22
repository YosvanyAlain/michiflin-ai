"""Security regression for pre-existing server operations; no real accounts or network."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from michiflin import access, admin, chat, community, config, identity_guard, live_web, miniapp, state

OWNER = "owner@example.org"


def test_official_connections_require_an_unchanged_verified_binding(monkeypatch):
    entry = {'chat': 7, 'addr': OWNER, 'epoch': 'fixture', 'build': 'current', 'access_session': 'one'}
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', False)
    monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', False)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', False)
    identity = Mock(return_value=(dict(entry), OWNER)); monkeypatch.setattr(live_web, '_identity', identity)
    monkeypatch.setattr(miniapp, 'compatible_build', lambda _entry: True)
    assert not miniapp._official_private(100, entry)  # unverified deployments fail closed
    identity.assert_not_called()
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', True)
    assert miniapp._official_private(100, entry)
    for field, changed in [('chat', 8), ('addr', 'other@example.org'), ('epoch', 'other'), ('build', 'old'), ('access_session', 'two')]:
        modified = dict(entry, **{field: changed})
        identity.return_value = (modified, OWNER)
        assert not miniapp._official_private(100, entry), field
    identity.return_value = (dict(entry), 'other@example.org')
    assert not miniapp._official_private(100, entry)
    identity.return_value = None  # group, unencrypted, changed key, or wrong sender
    assert not miniapp._official_private(100, entry)


def test_unverified_social_request_is_not_admitted_or_acknowledged(monkeypatch):
    monkeypatch.setattr(miniapp, '_release_allows', lambda *_args: True)
    monkeypatch.setattr(miniapp, '_official_private', lambda *_args: False)
    admit = Mock(); monkeypatch.setattr(miniapp, '_admit', admit)
    reply = Mock(); monkeypatch.setattr(miniapp, 'respond', reply)
    for op in miniapp.OFFICIAL_ACCOUNT_OPS:
        miniapp._route(17, {'chat': 7, 'addr': OWNER}, {'id': 'request', 'op': op, 'args': {'do': 'start'}})
    admit.assert_not_called(); reply.assert_not_called()


def test_private_cached_answer_and_each_fragment_recheck_identity(monkeypatch):
    entry = {'chat': 7, 'addr': OWNER, 'epoch': 'fixture', 'build': 'current', 'access_session': 'one'}
    body = json.dumps({'t': 'res', 're': 'private', 'ok': True, 'data': {'result': 'sensitive ' * 20}})
    monkeypatch.setattr(miniapp, '_answers', {'private': {'body': body, 'official_entry': entry}})
    monkeypatch.setattr(miniapp, '_status_allowed', lambda _instance: True)
    monkeypatch.setattr(miniapp, '_release_allows', lambda *_args: True)
    monkeypatch.setattr(miniapp, '_admit', lambda *_args: 'cached')
    monkeypatch.setattr(access, 'bound_allowed', lambda _entry: True)
    monkeypatch.setattr(admin, 'is_allowed', lambda _address: True)
    allowed = [False]
    monkeypatch.setattr(miniapp, '_official_private', lambda _instance, _entry: allowed[0])
    sent = []
    def send(_instance, payload, _notify=None, *, interrupted=None):
        if interrupted and interrupted():
            return False
        sent.append(payload)
        allowed[0] = False  # revoke after the first fragment leaves
        return True
    monkeypatch.setattr(chat, 'send_update', send)
    monkeypatch.setattr(miniapp, 'CHUNK', 24)
    # Even if a retry lies about its operation, the cached original remains private.
    miniapp._route(17, entry, {'id': 'private', 'retry': 1, 'op': 'hello', 'args': {}})
    assert sent == []
    allowed[0] = True
    miniapp._route(17, entry, {'id': 'private', 'retry': 2, 'op': 'hello', 'args': {}})
    assert len(sent) == 1 and sent[0]['t'] == 'chunk'


def test_private_handoff_rechecks_after_waiting_for_send_lock(monkeypatch):
    monkeypatch.setattr(miniapp, '_status_allowed', lambda _instance: True)
    allowed, sent = [True], []
    def send(_instance, payload, _notify=None, *, interrupted=None):
        allowed[0] = False  # recipient changed after the outer check
        if not interrupted():
            sent.append(payload)
            return True
        return False
    monkeypatch.setattr(chat, 'send_update', send)
    assert not miniapp._send_status(17, {'code': 'sensitive'}, private_check=lambda: allowed[0])
    assert sent == []


class SyntheticIdentityRpc:
    """Real policy and binding checks around a disposable RPC-shaped fixture."""
    def __init__(self, address, chat_id):
        self.info = SimpleNamespace(chat_type='Single', is_encrypted=True,
                                    is_contact_request=False, is_self_talk=False)
        self.contact = SimpleNamespace(address=address, is_key_contact=True, e2ee_avail=True,
                                       is_verified=True, verifier_id=1, fingerprint='A' * 40)
        self.members = [15]
        self.message = SimpleNamespace(chat_id=chat_id, from_id=1)
        self.sent = []

    def get_message(self, _account, _instance):
        return self.message

    def get_basic_chat_info(self, _account, _chat_id):
        return self.info

    def get_chat_contacts(self, _account, _chat_id):
        return self.members

    def get_contact(self, _account, _contact_id):
        return self.contact

    def send_webxdc_status_update(self, _account, instance, payload, _description):
        self.sent.append((instance, payload))


@pytest.fixture
def live_identity(monkeypatch):
    """Exercise _official_private -> Live -> identity_guard without mocking them."""
    monkeypatch.setattr(config, 'OWNER_ADDRESS', OWNER)
    monkeypatch.setattr(config, 'OWNER_BACKUP_ADDRESS', '')
    monkeypatch.setattr(config, 'OWNER_PGP_FINGERPRINT', 'A' * 40)
    monkeypatch.setattr(config, 'OWNER_ONLY', False)
    monkeypatch.setattr(config, 'MAINTENANCE_MODE', False)
    monkeypatch.setattr(config, 'BOT_EPOCH', 'synthetic-epoch')
    monkeypatch.setattr(config, 'BOT_EPOCH_STRICT', True)
    monkeypatch.setattr(config, 'REVOKE_LEGACY_MINIAPPS', True)
    monkeypatch.setattr(config, 'MINIAPP_CURRENT_ONLY', True)
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'current-package')
    monkeypatch.setattr(access, '_controlled', False)
    monkeypatch.setattr(access, '_guests', {})
    monkeypatch.setattr(community, '_pending', set())
    monkeypatch.setattr(identity_guard, 'contact_fingerprint',
                        lambda rpc, _account, _contact: rpc.contact.fingerprint)
    rpc = SyntheticIdentityRpc(OWNER, 21)
    entry = {'chat': 21, 'addr': OWNER, 'epoch': 'synthetic-epoch',
             'build': 'current-package', 'access_session': 'owner-session'}
    monkeypatch.setattr(state, 'data', {'apps': {'100': entry}, 'invitees': {},
        'email_key_bindings': {OWNER: {'key': identity_guard._key_digest('A' * 40),
                                       'chat': 21, 'retired': []}}})
    monkeypatch.setattr(chat, '_bot', SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(chat, '_accid', 1)
    return rpc, entry


@pytest.mark.parametrize('verified,recovery,invites', list(product([False, True], repeat=3)))
def test_real_identity_policy_matrix_for_official_owner(live_identity, monkeypatch, verified, recovery, invites):
    rpc, entry = live_identity
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', verified)
    monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', recovery)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', invites)
    # Invitation mode alone verifies guests, but cannot authenticate an owner
    # without the strict owner pin or an already-bound recovery key.
    expected = verified or recovery
    assert miniapp._official_private(100, dict(entry)) is expected
    if not expected:
        return
    # Even an accepted email-recovery key does not permit plaintext, groups,
    # foreign senders, a different app message, or a replacement grant.
    for obj, attribute, bad in [(rpc.info, 'chat_type', 'Group'),
                                 (rpc.info, 'is_encrypted', False),
                                 (rpc.info, 'is_contact_request', True),
                                 (rpc.info, 'is_self_talk', True),
                                 (rpc.contact, 'address', 'other@example.invalid'),
                                 (rpc.contact, 'is_key_contact', False),
                                 (rpc.contact, 'e2ee_avail', False),
                                 (rpc.message, 'from_id', 99),
                                 (rpc.message, 'chat_id', 22)]:
        old = getattr(obj, attribute)
        setattr(obj, attribute, bad)
        assert not miniapp._official_private(100, dict(entry)), attribute
        setattr(obj, attribute, old)
    rpc.members = [15, 16]
    assert not miniapp._official_private(100, dict(entry))
    rpc.members = [15]
    assert not miniapp._official_private(100, dict(entry, access_session='another-grant'))
    assert not miniapp._official_private(100, dict(entry, build='an-old-package'))
    # Email recovery deliberately permits a currently bound encrypted key that
    # is not directly verified. Strict pinning alone does not; document this.
    rpc.contact.is_verified = False
    rpc.contact.verifier_id = None
    assert miniapp._official_private(100, dict(entry)) is recovery
    if recovery:
        state.data['email_key_bindings'][OWNER]['key'] = identity_guard._key_digest('B' * 40)
        assert not miniapp._official_private(100, dict(entry))


@pytest.mark.parametrize('verified,recovery', list(product([False, True], repeat=2)))
def test_invited_guest_always_needs_direct_verification_and_original_grant(live_identity, monkeypatch, verified, recovery):
    rpc, entry = live_identity
    guest, stamp = 'guest@example.invalid', 'a' * 32
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', verified)
    monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', recovery)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', True)
    rpc.contact.address = guest
    entry.update(addr=guest, access_session=stamp)
    state.data['invitees'][guest] = {'active': True, 'banned': False, 'chat': 21,
                                     'key': identity_guard._key_digest('A' * 40), 'stamp': stamp}
    assert miniapp._official_private(100, dict(entry))
    rpc.contact.is_verified = False
    rpc.contact.verifier_id = None
    assert not miniapp._official_private(100, dict(entry))  # email recovery never substitutes for guest SecureJoin
    rpc.contact.is_verified, rpc.contact.verifier_id = True, 1
    state.data['invitees'][guest]['stamp'] = 'b' * 32
    assert not miniapp._official_private(100, dict(entry))
    state.data['invitees'][guest]['stamp'] = stamp
    state.data['invitees'][guest]['key'] = identity_guard._key_digest('B' * 40)
    assert not miniapp._official_private(100, dict(entry))
    state.data['invitees'][guest]['key'] = identity_guard._key_digest('A' * 40)
    state.data['invitees'][guest]['active'] = False
    assert not miniapp._official_private(100, dict(entry))


def test_real_handoff_lock_rechecks_identity_after_waiting(live_identity, monkeypatch):
    rpc, entry = live_identity
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', True)
    monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', False)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', False)
    entered = threading.Event()
    actual_send = chat.send_update

    def entered_send(*args, **kwargs):
        entered.set()
        return actual_send(*args, **kwargs)

    monkeypatch.setattr(chat, 'send_update', entered_send)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with chat._lock:
            future = pool.submit(miniapp._send_status, 100, {'test': 'sensitive fixture'},
                                 private_check=lambda: miniapp._official_private(100, dict(entry)))
            assert entered.wait(3)  # initial authorization passed, real chat lock is held
            rpc.info.is_encrypted = False
        assert future.result(timeout=5) is False
    assert rpc.sent == []
