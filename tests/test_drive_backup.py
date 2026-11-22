"""Synthetic Drive HTTP: never uses the connected app or a real refresh token."""
import hashlib
from unittest.mock import Mock

import pytest

import drive_backup as drive
import state_backup as backup


@pytest.fixture
def mirror(monkeypatch):
    monkeypatch.setenv('DRIVE_BACKUP_ENABLED', '1')
    for key in drive.KEYS:
        monkeypatch.setenv(key, 'synthetic_value_12345')
    monkeypatch.setattr(drive, '_headers', lambda: {'Authorization': 'Bearer synthetic'})
    envelope = backup.MAGIC + b'synthetic encrypted data'
    decrypt = Mock(return_value=b'synthetic plaintext archive')
    verify = Mock()
    monkeypatch.setattr(backup, 'decrypt_backup', decrypt)
    monkeypatch.setattr(backup, '_verify_plaintext', verify)
    folder = Mock()
    folder.json.return_value = {'id': 'synthetic_value_12345', 'mimeType': 'application/vnd.google-apps.folder', 'shared': False}
    uploaded = Mock(); uploaded.json.return_value = {'id': 'synthetic_file_12345'}
    ready = Mock(); ready.json.return_value = {'id': 'synthetic_file_12345', 'name': 'snapshot.bin', 'appProperties': {'verified': 'true'}}
    get = Mock(return_value=folder); post = Mock(return_value=uploaded); patch = Mock(return_value=ready)
    monkeypatch.setattr(drive.requests, 'get', get)
    monkeypatch.setattr(drive.requests, 'post', post)
    monkeypatch.setattr(drive.requests, 'patch', patch)
    download = Mock(return_value=envelope)
    monkeypatch.setattr(drive, '_download', download)
    return envelope, get, post, patch, decrypt, verify, download


def test_encrypted_additional_copy_readback_and_recovery_verified(mirror):
    envelope, get, post, patch, decrypt, verify, _ = mirror
    result = drive.mirror(envelope, 'snapshot.bin', 'synthetic passphrase')
    assert result['status'] == 'verified'
    assert result['sha256'] == hashlib.sha256(envelope).hexdigest()
    payload = post.call_args.kwargs['data']
    assert envelope in payload and b'synthetic plaintext archive' not in payload
    assert decrypt.call_count == verify.call_count == 2
    assert patch.call_args.kwargs['json']['name'] == 'snapshot.bin'
    assert get.call_args.kwargs['allow_redirects'] is False


def test_drive_disabled_does_not_touch_network(mirror, monkeypatch):
    envelope, get, post, patch, _, _, _ = mirror
    monkeypatch.setenv('DRIVE_BACKUP_ENABLED', '0')
    assert drive.mirror(envelope, 'snapshot.bin', 'pass')['status'] == 'disabled'
    get.assert_not_called(); post.assert_not_called(); patch.assert_not_called()


def test_missing_credentials_inactive_and_no_upload(mirror, monkeypatch):
    envelope, get, post, _, _, _, _ = mirror
    monkeypatch.delenv(drive.KEYS[2])
    assert not drive.configured()
    with pytest.raises(ValueError, match='incomplete'):
        drive.mirror(envelope, 'snapshot.bin', 'pass')
    get.assert_not_called(); post.assert_not_called()


@pytest.mark.parametrize('field,value', [('shared', True), ('shared', None), ('trashed', True), ('mimeType', 'text/plain')])
def test_shared_or_invalid_folder_never_receives_state(mirror, field, value):
    envelope, get, post, _, _, _, _ = mirror
    get.return_value.json.return_value[field] = value
    with pytest.raises(ValueError, match='unshared'):
        drive.mirror(envelope, 'snapshot.bin', 'pass')
    post.assert_not_called()


def test_readback_mismatch_never_marks_ready(mirror):
    envelope, _, _, patch, _, _, download = mirror
    download.return_value = b'corrupt'
    with pytest.raises(ValueError, match='mismatch'):
        drive.mirror(envelope, 'snapshot.bin', 'pass')
    patch.assert_not_called()


def test_invalid_recovery_keeps_pending_and_all_previous_backups(mirror):
    envelope, _, _, patch, _, verify, _ = mirror
    verify.side_effect = [None, ValueError('bad recovery')]
    with pytest.raises(ValueError, match='bad recovery'):
        drive.mirror(envelope, 'snapshot.bin', 'pass')
    patch.assert_not_called()


def test_plaintext_is_rejected_before_network(mirror):
    _, get, post, _, _, _, _ = mirror
    with pytest.raises(ValueError, match='encrypted'):
        drive.mirror(b'identity secrets in plaintext', 'snapshot.bin', 'pass')
    get.assert_not_called(); post.assert_not_called()


def test_explicit_recovery_returns_verified_archive_not_live_restore(mirror):
    _, _, _, _, _, verify, _ = mirror
    assert drive.recovery_archive('synthetic_file_12345', 'pass') == b'synthetic plaintext archive'
    verify.assert_called_once()


def test_invalid_ids_do_not_become_urls(mirror):
    with pytest.raises(ValueError, match='invalid Drive id'):
        drive.recovery_archive('../external?token=x', 'pass')


def test_streamed_download_enforces_limit(monkeypatch):
    response = Mock(status_code=200)
    response.iter_content.return_value = [b'1234', b'5678']
    context = Mock(); context.__enter__ = Mock(return_value=response); context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(drive.requests, 'get', Mock(return_value=context))
    with pytest.raises(ValueError, match='limit'):
        drive._download('synthetic_file_12345', {}, 7)


@pytest.mark.parametrize('failure', [ValueError('missing credentials'), drive.requests.Timeout('synthetic')])
def test_mirror_failure_preserves_successful_primary_and_suppresses_rotation(monkeypatch, failure):
    monkeypatch.setenv('DRIVE_BACKUP_ENABLED', '1')
    monkeypatch.delenv('STATE_BACKUP_NAMESPACE', raising=False)
    uploaded = {}
    def upload(*args, **kwargs):
        uploaded['bytes'] = kwargs['data']
        return Mock(json=lambda: {'id': 123})
    def rename(*args, **kwargs):
        return Mock(json=lambda: {'id': 123, 'name': kwargs['json']['name'], 'state': 'uploaded'})
    monkeypatch.setattr(backup, '_assert_private', Mock())
    monkeypatch.setattr(backup, 'archive', Mock(return_value=b'synthetic complete archive'))
    monkeypatch.setattr(backup, '_ensure_release', Mock(return_value={'id': 7}))
    monkeypatch.setattr(backup.requests, 'post', upload)
    monkeypatch.setattr(backup.requests, 'patch', rename)
    monkeypatch.setattr(backup, '_download_asset', lambda *args: uploaded['bytes'])
    monkeypatch.setattr(backup, '_verify_plaintext', Mock())
    primary_ready = Mock(); monkeypatch.setattr(backup, '_mark_ready', primary_ready)
    prune = Mock(); monkeypatch.setattr(backup, '_prune', prune)
    monkeypatch.setattr(drive, 'mirror', Mock(side_effect=failure))
    backup._save_release('synthetic', 'owner/private', 'synthetic passphrase')
    primary_ready.assert_called_once()
    prune.assert_not_called()
