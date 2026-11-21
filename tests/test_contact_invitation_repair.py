"""Synthetic fixtures only: no real link, identity or mailbox."""
import sqlite3
from unittest.mock import Mock

import pytest

from tools import repair_contact_invitation_once as tool

FP = 'A' * 40
INVITE = 'synthetic_invite_number_1234'
AUTH = 'synthetic_auth_code_12345678'
LINK = f'https://i.delta.chat/#{FP}&v=3&i={INVITE}&s={AUTH}&a=fixture%40example.org&n=Fixture'


@pytest.fixture
def account(tmp_path):
    p = tmp_path / 'accounts' / '1' / 'dc.db'
    p.parent.mkdir(parents=True)
    with sqlite3.connect(p) as db:
        db.executescript('''CREATE TABLE config(keyname TEXT,value TEXT);
        CREATE TABLE keypairs(public_key BLOB);
        CREATE TABLE tokens(id INTEGER PRIMARY KEY,namespc INTEGER,foreign_key TEXT,token TEXT,timestamp INTEGER);
        CREATE TABLE msgs(id INTEGER PRIMARY KEY,txt TEXT);''')
        db.execute("INSERT INTO config VALUES('configured_addr','fixture@example.org')")
        db.execute('INSERT INTO keypairs VALUES(?)', (b'synthetic-public-key',))
        db.execute("INSERT INTO tokens(namespc,foreign_key,token,timestamp) VALUES(100,'',?,1)", (INVITE,))
        db.execute("INSERT INTO msgs VALUES(1,'preserved history')")
    return tmp_path, p


def tokens(p):
    with sqlite3.connect(p) as db:
        return db.execute('SELECT namespc,foreign_key,token,timestamp FROM tokens ORDER BY id').fetchall()


def test_restore_only_given_auth_and_checkpoint_idempotently(account):
    root, p = account
    checkpoint = Mock()
    result = tool.repair(root, LINK, checkpoint, lambda key: FP)
    assert result['auth_token_restored'] and result['encrypted_checkpoint_verified']
    assert not result['mail_io_started'] and not result['identity_replaced']
    assert tokens(p)[0] == (100, '', INVITE, 1)
    assert tokens(p)[1][:3] == (110, '', AUTH)
    first = tokens(p)
    assert not tool.repair(root, LINK, checkpoint, lambda key: FP)['auth_token_restored']
    assert tokens(p) == first
    with sqlite3.connect(p) as db:
        assert db.execute('SELECT txt FROM msgs').fetchone()[0] == 'preserved history'
        assert db.execute('SELECT public_key FROM keypairs').fetchone()[0] == b'synthetic-public-key'
    assert checkpoint.call_count == 2
    assert AUTH not in str(result) and INVITE not in str(result)


@pytest.mark.parametrize('bad', [LINK.replace(FP, 'B' * 40), LINK.replace('fixture%40', 'wrong%40'),
                                LINK.replace(INVITE, 'unregistered_invite_123456'),
                                LINK.replace('https:', 'http:'), LINK + '&i=duplicate', LINK + '&x=group'])
def test_mismatched_or_group_invitation_never_changes_state(account, bad):
    root, p = account
    before = tokens(p)
    checkpoint = Mock()
    with pytest.raises(ValueError):
        tool.repair(root, bad, checkpoint, lambda key: FP)
    assert tokens(p) == before
    checkpoint.assert_not_called()


def test_backup_failure_removes_only_new_token(account):
    root, p = account
    before = tokens(p)
    with pytest.raises(RuntimeError):
        tool.repair(root, LINK, Mock(side_effect=RuntimeError('synthetic')), lambda key: FP)
    assert tokens(p) == before


def test_existing_auth_preserved_on_backup_failure(account):
    root, p = account
    tool.repair(root, LINK, Mock(), lambda key: FP)
    before = tokens(p)
    with pytest.raises(RuntimeError):
        tool.repair(root, LINK, Mock(side_effect=RuntimeError('synthetic')), lambda key: FP)
    assert tokens(p) == before


def test_exact_revision_required_before_loading_account(monkeypatch):
    monkeypatch.setenv('INVITATION_REPAIR_REVISION', '1' * 40)
    monkeypatch.setenv('RUNNING_SHA', '2' * 40)
    with pytest.raises(ValueError, match='not armed'):
        tool.run()
