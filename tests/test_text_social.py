"""Private synthetic SQLite community: no live contacts, network or credentials."""
import json
import sqlite3
from unittest.mock import Mock

import pytest

from michiflin import access, admin, config, text_social as social


@pytest.fixture
def community(monkeypatch, tmp_path):
    grants = {'one@private.invalid': 'grant-1', 'two@private.invalid': 'grant-2', 'owner@private.invalid': 'owner'}
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(config, 'BOT_EPOCH', 'test-epoch')
    monkeypatch.setattr(config, 'BOT_EPOCH_STRICT', True)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', True)
    monkeypatch.setattr(config, 'is_owner_admin', lambda address: address == 'owner@private.invalid')
    monkeypatch.setattr(config, 'service_access_allowed', lambda address: address in grants)
    monkeypatch.setattr(admin, 'is_allowed', lambda address: address in grants)
    monkeypatch.setattr(access, 'session_stamp', lambda address: grants.get(address, ''))
    clock = Mock(return_value=10000.)
    monkeypatch.setattr(social.time, 'time', clock)
    def call(action='feed', who='one', **kwargs):
        return social.operate(who + '@private.invalid', {'do': action, **kwargs})
    return call, grants, clock


def post(call, text='Synthetic text #hello', who='one', nonce='synthetic_nonce', **kwargs):
    return call('post', who=who, text=text, nonce=nonce, **kwargs)['created_id']


def test_alias_profile_and_public_privacy(community):
    call, _, _ = community
    response = call('profile', alias='Luna', bio='A short bio', prefs={'theme': 'ocean', 'font': 'large'})
    assert response['me']['alias'] == 'Luna'
    assert response['prefs']['theme'] == 'ocean'
    post(call)
    public = json.dumps(call(who='two'))
    assert 'Luna' in public and 'private.invalid' not in public and 'scope' not in public and 'grant-' not in public
    with pytest.raises(social.SocialError, match='alias_taken'):
        call('profile', who='two', alias='lUnA')
    with pytest.raises(social.SocialError, match='invalid_alias'):
        call('profile', alias='a@b.example')


@pytest.mark.parametrize('args', [
    {'alias': 'admin helper'}, {'alias': 'michiflin'}, {'alias': '<script>'},
    {'prefs': {'theme': []}}, {'prefs': {'font': 'enormous'}}, {'prefs': {'unknown': True}},
    {'prefs': {'muted_tags': ['x y']}}, {'prefs': {'muted_tags': ['a'] * 31}}, {'bio': '\0'},
])
def test_bad_profiles_are_rolled_back(community, args):
    call, _, _ = community
    before = call('me')['me']
    with pytest.raises(social.SocialError):
        call('profile', **args)
    assert call('me')['me'] == before


def test_nonce_exact_retry_and_conflict(community):
    call, _, _ = community
    created = post(call)
    response = call('post', text='Synthetic text #hello', nonce='synthetic_nonce')
    assert response == {'created_id': created, 'duplicate': True}
    assert len(call()['posts']) == 1
    with pytest.raises(social.SocialError, match='conflict'):
        post(call, 'Changed text')


def test_rate_limits_are_per_person_and_capacity_never_purges(community, monkeypatch):
    call, _, _ = community
    post(call)
    with pytest.raises(social.SocialError, match='rate_limit'):
        post(call, nonce='new_nonce_123')
    post(call, who='two')
    monkeypatch.setattr(social, 'MAX_POSTS', 2)
    with pytest.raises(social.SocialError, match='capacity'):
        post(call, who='owner')
    assert len(call()['posts']) == 2


def test_revision_ownership_deletion_and_text_safety(community):
    call, _, _ = community
    pid = post(call, '<script>alert(1)</script>\nplain text')
    with pytest.raises(social.SocialError, match='denied'):
        call('edit', who='two', id=pid, revision=1, text='steal')
    call('edit', id=pid, revision=1, text='Updated')
    with pytest.raises(social.SocialError, match='conflict'):
        call('edit', id=pid, revision=1, text='Stale')
    assert call()['posts'][0]['text'] == 'Updated'
    call('delete', id=pid, revision=2)
    assert call()['posts'] == []
    with social.database() as db:
        assert db.execute('SELECT body FROM posts WHERE id=?', (pid,)).fetchone()[0] == ''


def test_threads_saved_replies_and_hidden_parent(community):
    call, _, _ = community
    root = post(call)
    reply = post(call, 'A reply', who='two', parent=root)
    assert [r['id'] for r in call('thread', parent=root)['posts']] == [reply]
    call('save', id=reply, on=True)
    assert call(feed='saved')['posts'][0]['id'] == reply
    call('hide', id=root, on=True)
    assert call(feed='saved')['posts'] == []
    with pytest.raises(social.SocialError, match='not_found'):
        call('thread', parent=root)
    call('restore_hidden')
    assert call('thread', parent=root)['root']['id'] == root
    with pytest.raises(social.SocialError):
        call('thread', parent=reply)


def test_follow_mute_tags_like_and_restore(community):
    call, _, _ = community
    pid = post(call, who='two')
    person = call()['posts'][0]['author']['id']
    assert not call(feed='following')['posts']
    call('follow', person=person, on=True)
    assert call(feed='following')['posts'][0]['id'] == pid
    call('like', id=pid, on=True); call('like', id=pid, on=True)
    assert call()['posts'][0]['likes'] == 1
    call('like', id=pid, on=False)
    assert call()['posts'][0]['likes'] == 0
    call('profile', prefs={'muted_tags': ['HELLO']})
    assert not call()['posts']
    call('profile', prefs={})
    call('mute', person=person, on=True)
    assert not call()['posts']
    call('restore_hidden')
    assert call()['posts']


def test_moderation_owner_only_reports_and_bans(community):
    call, _, _ = community
    pid = post(call)
    author = call()['me']['id']
    call('report', who='two', id=pid, reason='Synthetic report')
    with pytest.raises(social.SocialError, match='denied'):
        call('moderation', who='two')
    with pytest.raises(social.SocialError, match='denied'):
        call('moderate', who='two', id=pid, on=True)
    call('pin', who='owner', id=pid, on=True)
    assert call(feed='pinned')['posts'][0]['pinned']
    call('moderate', who='owner', id=pid, on=True)
    assert not call()['posts']
    assert call(who='owner')['posts'][0]['hidden']
    assert call('moderation', who='owner')['reports'][0]['resolved']
    call('moderate', who='owner', id=pid, on=False)
    call('ban', who='owner', person=author, on=True)
    with pytest.raises(social.SocialError, match='suspended'):
        call()
    assert not call(who='two')['posts']
    call('ban', who='owner', person=author, on=False)
    assert call()['posts']


def test_banned_root_does_not_leak_through_saved_reply(community):
    call, _, _ = community
    root = post(call)
    author = call()['me']['id']
    reply = post(call, who='two', parent=root)
    call('save', who='two', id=reply, on=True)
    call('ban', who='owner', person=author, on=True)
    assert not call(who='two', feed='saved')['posts']


def test_grant_revocation_new_grant_and_epoch_scope(community, monkeypatch):
    call, grants, _ = community
    old = call()['me']['id']
    grants.pop('one@private.invalid')
    with pytest.raises(social.SocialError, match='denied'):
        call()
    grants['one@private.invalid'] = 'new-grant'
    new = call()['me']['id']
    assert new != old
    monkeypatch.setattr(config, 'BOT_EPOCH', 'new-epoch')
    assert call()['me']['id'] != new


@pytest.mark.parametrize('args', [
    {'do': 'post', 'text': 'x', 'nonce': '../bad'}, {'do': 'post', 'text': 'x' * 3001, 'nonce': 'a' * 8},
    {'do': 'thread', 'parent': True}, {'do': 'thread', 'parent': 0}, {'do': 'like', 'id': '1', 'on': True},
    {'do': 'feed', 'before': 0}, {'do': []}, {'do': 'feed', 'feed': []},
])
def test_input_validation(community, args):
    with pytest.raises(social.SocialError):
        social.operate('one@private.invalid', args)


def test_pagination_search_and_database_permissions(community):
    call, _, clock = community
    for i in range(25):
        clock.return_value += 40
        post(call, text=f'Message {i}', nonce=f'nonce_number_{i}')
    first = call()
    second = call(before=first['next'])
    assert len(first['posts']) == 20 and len(second['posts']) == 5
    assert not {p['id'] for p in first['posts']} & {p['id'] for p in second['posts']}
    assert len(call(q='Message 24')['posts']) == 1
    path = config.CONFIG_DIR / 'text-social.sqlite3'
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_community_is_in_consistent_encrypted_backup(community):
    import io
    import tarfile
    import state_backup as backup
    call, _, _ = community
    post(call)
    archive = backup.archive(config.CONFIG_DIR)
    backup._verify_plaintext(archive)
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as data:
        names = data.getnames()
        assert 'michiflin/text-social.sqlite3' in names
        assert not any(n.endswith(('-wal', '-shm')) for n in names)
