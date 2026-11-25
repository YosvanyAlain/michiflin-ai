"""Synthetic, offline release-delivery safety tests; no real account or mail."""
from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from michiflin import access, chat, community, config, identity_guard, miniapp, release_selection, state
from tools import deliver_release_once as delivery

OWNER = 'owner@example.invalid'
ALICE = 'alice@example.invalid'
BOB = 'bob@example.invalid'
FP = {'owner@example.invalid': 'A' * 40, 'alice@example.invalid': 'B' * 40, 'bob@example.invalid': 'C' * 40}
REVISION = 'd' * 40
EPOCH = 'e' * 48
BUILD = 'abcdef1234'


class SyntheticRPC:
    def __init__(self):
        self.contacts = {OWNER: 11, ALICE: 12, BOB: 13}
        self.chats = {11: 21, 12: 22, 13: 23}
        self.blocked = set()
        self.bad_keys = set()
        self.unverified = set()
        self.unencrypted = set()
        self.pending = []
        self.messages = {}
        self.started = 0
        self.stopped = 0

    def lookup_contact_id_by_addr(self, account, address):
        assert account == 1
        return self.contacts.get(address)

    def get_chat_id_by_contact_id(self, _account, cid):
        return self.chats.get(cid)

    def get_blocked_contacts(self, _account):
        return [SimpleNamespace(id=cid) for cid in self.blocked]

    def get_contact(self, _account, cid):
        address = next(a for a, number in self.contacts.items() if number == cid)
        return SimpleNamespace(address=address, is_key_contact=True, e2ee_avail=True,
                               is_verified=address not in self.unverified, verifier_id=1)

    def get_basic_chat_info(self, _account, cid):
        return SimpleNamespace(chat_type='Single', is_encrypted=cid not in self.unencrypted,
                               is_self_talk=False, is_contact_request=False)

    def get_chat_contacts(self, _account, cid):
        return [next(key for key, value in self.chats.items() if value == cid)]

    def start_io(self, _account):
        self.started += 1

    def stop_io(self, _account):
        self.stopped += 1

    def get_next_msgs(self, _account):
        return list(self.pending)

    def get_message(self, _account, mid):
        return self.messages[mid]


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    original = copy.deepcopy(state.data)
    access.reset()
    for key, value in {
        'OWNER_ADDRESS': OWNER, 'OWNER_BACKUP_ADDRESS': '', 'OWNER_ONLY': False,
        'INVITE_AUTO_ACCESS': True, 'BOT_VERIFIED_CONTACTS_ONLY': True, 'EMAIL_KEY_RECOVERY': False,
        'BOT_EPOCH': EPOCH, 'BOT_EPOCH_STRICT': True, 'MINIAPP_CURRENT_ONLY': True,
        'REVOKE_LEGACY_MINIAPPS': True, 'MAINTENANCE_MODE': False,
        'OWNER_PGP_FINGERPRINT': FP[OWNER],
    }.items():
        monkeypatch.setattr(config, key, value)
    for key, value in {
        'RUNNING_SHA': REVISION, 'RELEASE_APP_SEND_REVISION': REVISION, 'CONFIRM_RELEASE_DELIVERY': '1',
        'GITHUB_REPOSITORY': 'YosvanyAlain/michiflin-ai', 'GITHUB_REF': 'refs/heads/main',
        'GITHUB_RUN_ATTEMPT': '1', 'STATE_BACKUP_REQUIRED': '1', 'STATE_BACKUP_STORAGE': 'releases',
        'STATE_BACKUP_NAMESPACE': 'owner-clean-v1', 'STATE_BACKUP_FENCE_REQUIRED': '1',
        'STATE_BACKUP_RETAIN_ALL': '1', 'STATE_BACKUP_INCLUDE_BLOBS': '1',
    }.items():
        monkeypatch.setenv(key, value)
    state.data.update({'apps': {}, 'invitees': {}, 'invite_policy': {'closed': False, 'until': 0.0},
                       'settings': {}, 'prefs': {}, 'inbox': {}, 'flags': {}, 'kill_switch': {},
                       'access': {'allowed': ['old@example.invalid'], 'blocked': [], 'attempts': {}, 'alerts': True}})
    state.data.pop(delivery.LEDGER, None)
    for address, cid in ((ALICE, 22), (BOB, 23)):
        state.data['invitees'][address] = {'active': True, 'banned': False, 'chat': cid,
                                           'key': identity_guard._key_digest(FP[address]),
                                           'stamp': ('a' if address == ALICE else 'b') * 32}
    access.restore_policy()
    community._pending.clear()
    rpc = SyntheticRPC()
    monkeypatch.setattr(identity_guard, 'contact_fingerprint', lambda _rpc, _acc, cid:
                        ('F' * 40 if cid in rpc.bad_keys else FP[rpc.get_contact(1, cid).address]))
    monkeypatch.setattr(state, 'save', lambda: None)
    monkeypatch.setattr(miniapp, 'app_build', lambda: BUILD)
    monkeypatch.setattr(delivery, '_package', lambda: 'f' * 64)
    monkeypatch.setattr(miniapp, 'build', lambda: tmp_path / 'synthetic.xdc')
    chat.bind(SimpleNamespace(rpc=rpc), 1)
    snapshots, sends = [], []
    def backup():
        snapshots.append(copy.deepcopy(state.data))
    def fake_send(cid, text='', **kwargs):
        assert snapshots and any(item['state'] == 'attempting' for item in
                                 snapshots[-1][delivery.LEDGER][delivery.RELEASE_ID]['recipients'].values())
        if not chat.recipient_allowed(cid) or kwargs.get('interrupted', lambda: False)():
            return 0
        assert kwargs['viewtype'] == 'Webxdc' and kwargs['filename'] == 'Michiflin AI.xdc'
        mid = 1000 + len(sends)
        sends.append((cid, text, mid))
        rpc.messages[mid] = SimpleNamespace(show_padlock=True, from_id=1, is_info=False)
        rpc.pending.append(mid)  # own outgoing mail is not an unseen instruction
        return mid
    monkeypatch.setattr(chat, 'send', fake_send)
    monkeypatch.setattr(chat, 'wait_delivery', lambda *_: 'delivered')
    external = {'used': False}
    def acquire():
        if external['used']:
            raise delivery.StopSafely('once_marker_already_present')
        external['used'] = True
    result = SimpleNamespace(rpc=rpc, snapshots=snapshots, sends=sends, backup=backup,
                             acquire=acquire, external=external, tmp=tmp_path)
    yield result
    state.data.clear(); state.data.update(original)
    community._pending.clear()
    access.reset()


def run(fixture):
    return delivery.deliver(fixture.rpc, 1, fixture.backup, timeout=0,
                            acquire=fixture.acquire, wait_mail=lambda *_: None)


def test_selection_includes_active_grants_without_any_prior_app(fixture):
    chosen = release_selection.select(fixture.rpc, 1, BUILD)
    assert [x.address for x in chosen] == [OWNER, ALICE, BOB]
    assert not any(x.already_current for x in chosen)
    assert state.data['apps'] == {}  # no invented old binding required


@pytest.mark.parametrize('problem', ['revoked', 'blocked', 'unverified', 'key_changed', 'unencrypted', 'wrong_chat', 'stale_stamp'])
def test_revoked_or_unverified_people_are_excluded(fixture, problem):
    if problem == 'revoked': state.data['invitees'][ALICE]['active'] = False
    if problem == 'blocked': fixture.rpc.blocked.add(12)
    if problem == 'unverified': fixture.rpc.unverified.add(ALICE)
    if problem == 'key_changed': fixture.rpc.bad_keys.add(12)
    if problem == 'unencrypted': fixture.rpc.unencrypted.add(22)
    if problem == 'wrong_chat': state.data['invitees'][ALICE]['chat'] = 99
    if problem == 'stale_stamp': state.data['invitees'][ALICE]['stamp'] = 'invalid'
    assert ALICE not in [item.address for item in release_selection.select(fixture.rpc, 1, BUILD)]
    assert OWNER in [item.address for item in release_selection.select(fixture.rpc, 1, BUILD)]


def test_current_registration_suppresses_duplicate_but_keeps_old_data(fixture):
    state.data['apps']['77'] = {'addr': ALICE, 'chat': 22, 'epoch': EPOCH, 'build': BUILD,
                                'access_session': 'a' * 32, 'locale': 'en', 'active': 10,
                                'language_observed': True, 'private_old_data': 'preserved'}
    selected = release_selection.select(fixture.rpc, 1, BUILD)
    alice = next(x for x in selected if x.address == ALICE)
    assert alice.already_current and alice.locale == 'en'
    report = run(fixture)
    assert report['skipped_current'] == 1 and [cid for cid, _, _ in fixture.sends] == [21, 23]
    assert state.data['apps']['77']['private_old_data'] == 'preserved'


def test_handoff_is_durable_owner_first_and_not_claimed_as_user_receipt(fixture):
    report = run(fixture)
    assert report['plan_complete'] and report['recipients'] == 3 and report['delivered'] == 3
    assert [x[0] for x in fixture.sends] == [21, 22, 23]
    assert fixture.rpc.started == fixture.rpc.stopped == 1
    assert len(state.data['apps']) == 3
    assert all('datos locales se conservan' in text and 'no se trasladan automáticamente' in text for _, text, _ in fixture.sends)
    log = json.dumps(report)
    for secret in (OWNER, ALICE, BOB, FP[OWNER], FP[ALICE]):
        assert secret not in log
    assert run(fixture) == report and len(fixture.sends) == 3


def test_restore_of_stale_planned_snapshot_cannot_duplicate_handoff(fixture):
    run(fixture)
    stale = copy.deepcopy(fixture.snapshots[0])  # plan existed, no handoff in that snapshot
    state.data.clear(); state.data.update(stale)
    assert all(item['state'] == 'planned' for item in state.data[delivery.LEDGER][delivery.RELEASE_ID]['recipients'].values())
    with pytest.raises(delivery.StopSafely, match='once_marker_already_present'):
        run(fixture)
    assert len(fixture.sends) == 3


def test_revocation_between_plan_and_send_prevents_guest_delivery(fixture):
    def backup():
        fixture.backup()
        if len(fixture.snapshots) == 1:
            state.data['invitees'][ALICE]['active'] = False
    result = delivery.deliver(fixture.rpc, 1, backup, timeout=0, acquire=fixture.acquire, wait_mail=lambda *_: None)
    assert result['skipped'] == 1 and [cid for cid, _, _ in fixture.sends] == [21, 23]


def test_interrupted_handoff_is_never_retried(fixture, monkeypatch):
    def uncertain(*_args, **_kwargs):
        raise OSError('synthetic uncertain send')
    monkeypatch.setattr(miniapp, 'send_app', uncertain)
    report = run(fixture)
    assert report['uncertain'] == 3 and fixture.sends == []
    assert run(fixture) == report and fixture.sends == []


def test_failed_checkpoint_prevents_any_handoff_and_claim_is_consumed(fixture):
    def backup():
        fixture.backup()
        if len(fixture.snapshots) == 2:
            raise OSError('offline')
    with pytest.raises(delivery.StopSafely, match='backup_checkpoint_failed'):
        delivery.deliver(fixture.rpc, 1, backup, acquire=fixture.acquire, wait_mail=lambda *_: None)
    assert fixture.external['used'] and not fixture.sends
    with pytest.raises(delivery.StopSafely, match='once_marker_already_present'):
        run(fixture)
    assert not fixture.sends


def test_new_incoming_request_stops_before_claim(fixture):
    fixture.rpc.pending.append(33)
    fixture.rpc.messages[33] = SimpleNamespace(from_id=12, is_info=False)
    def wait(rpc, account):
        delivery._require_empty_inbox(rpc, account)
    with pytest.raises(delivery.StopSafely, match='unprocessed_inbox'):
        delivery.deliver(fixture.rpc, 1, fixture.backup, acquire=fixture.acquire, wait_mail=wait)
    assert not fixture.external['used'] and not fixture.sends


@pytest.mark.parametrize('setting', ['revision', 'attempt', 'ref', 'blobs', 'strict', 'weak_recovery'])
def test_arming_fails_closed(fixture, monkeypatch, setting):
    if setting == 'revision': monkeypatch.setenv('RUNNING_SHA', 'a' * 40)
    if setting == 'attempt': monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
    if setting == 'ref': monkeypatch.setenv('GITHUB_REF', 'refs/heads/review')
    if setting == 'blobs': monkeypatch.setenv('STATE_BACKUP_INCLUDE_BLOBS', '0')
    if setting == 'strict': monkeypatch.setattr(config, 'BOT_EPOCH_STRICT', False)
    if setting == 'weak_recovery': monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', True)
    with pytest.raises(delivery.StopSafely, match='not_armed'):
        run(fixture)
    assert not fixture.sends and not fixture.external['used']


def test_outbox_preflight_never_starts_old_queued_mail(tmp_path):
    root = tmp_path / 'accounts' / 'one'; root.mkdir(parents=True)
    with sqlite3.connect(root / 'dc.db') as db:
        db.execute('create table msgs(state integer, deleted integer)')
        db.execute('insert into msgs values(26,0)')
    delivery._require_empty_outbox(tmp_path)
    with sqlite3.connect(root / 'dc.db') as db:
        db.execute('insert into msgs values(20,0)')
    with pytest.raises(delivery.StopSafely, match='outbox_not_empty'):
        delivery._require_empty_outbox(tmp_path)


def test_remote_once_tag_accepts_only_first_confirmed_atomic_creation(monkeypatch):
    import state_backup
    refs = {state_backup.RELEASE_TAG: 'a' * 40, state_backup.CLEAN_OWNER_READY_TAG: 'a' * 40}
    monkeypatch.setattr(state_backup, 'configuration', lambda: ('synthetic', 'owner/private', 'synthetic'))
    monkeypatch.setattr(state_backup, '_assert_private', lambda *_: None)
    monkeypatch.setattr(state_backup, '_release', lambda *_: {'id': 1})
    monkeypatch.setattr(state_backup, '_get_ref', lambda _token, _repo, tag: refs.get(tag))
    def post(_url, **kwargs):
        data = kwargs['json']; tag = data['ref'].removeprefix('refs/tags/')
        assert kwargs['allow_redirects'] is False
        if tag in refs:
            return SimpleNamespace(status_code=422)
        refs[tag] = data['sha']
        return SimpleNamespace(status_code=201, json=lambda: {'ref': data['ref'], 'object': {'sha': data['sha']}})
    monkeypatch.setattr(requests, 'post', post)
    delivery._acquire_once()
    with pytest.raises(delivery.StopSafely, match='already_present'):
        delivery._acquire_once()


def test_ambiguous_atomic_creation_does_not_authorize_send(monkeypatch):
    import state_backup
    refs = {state_backup.RELEASE_TAG: 'a' * 40, state_backup.CLEAN_OWNER_READY_TAG: 'a' * 40}
    monkeypatch.setattr(state_backup, 'configuration', lambda: ('synthetic', 'owner/private', 'synthetic'))
    monkeypatch.setattr(state_backup, '_assert_private', lambda *_: None)
    monkeypatch.setattr(state_backup, '_release', lambda *_: {'id': 1})
    monkeypatch.setattr(state_backup, '_get_ref', lambda _token, _repo, tag: refs.get(tag))
    def timeout(_url, **kwargs):
        refs[delivery.ONCE_TAG] = kwargs['json']['sha']  # server may have created it
        raise requests.Timeout('synthetic')
    monkeypatch.setattr(requests, 'post', timeout)
    with pytest.raises(delivery.StopSafely, match='uncertain_do_not_retry'):
        delivery._acquire_once()
    with pytest.raises(delivery.StopSafely, match='already_present'):
        delivery._acquire_once()


def test_release_workflow_is_revision_pinned_and_continues_if_uncertain():
    source = Path('.github/workflows/michiflin.yml').read_text()
    assert 'env.RELEASE_APP_SEND_REVISION == github.sha' in source
    assert 'CONFIRM_RELEASE_DELIVERY: "1"' in source
    assert 'python tools/deliver_release_once.py' in source
    assert source.index('run: python tools/open_private_invite_access.py') < source.index('python tools/deliver_release_once.py')
    assert source.index('python tools/deliver_release_once.py') < source.index('name: Private chats and restricted public group tools')
