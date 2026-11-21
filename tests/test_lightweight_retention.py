"""Synthetic storage only: no keys, accounts, messages or mail IO from production."""
import copy
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import state_backup
from michiflin import app_compat, chat, config, miniapp, queue
from michiflin.media_retention import account_blob, is_downloaded_media
from tools.lightweight_recovery import discard_media_files, hold_old_work


@pytest.mark.parametrize('name,expected', [('movie.mp4', True), ('movie.MP4.001', True), ('sound.opus', True),
    ('book.pdf', False), ('app.xdc', False), ('private-key.asc', False), ('avatar.png', False), ('dc.db', False)])
def test_media_selection_preserves_identity_and_nonmedia(name, expected):
    assert is_downloaded_media(name) is expected


def test_light_backup_excludes_media_but_keeps_apps_keys_database_and_notes(monkeypatch, tmp_path):
    monkeypatch.setenv('STATE_BACKUP_INCLUDE_MEDIA', '0')
    monkeypatch.setenv('STATE_BACKUP_INCLUDE_BLOBS', '1')
    for name in ['accounts/test-blobs/movie.mp4', 'accounts/test-blobs/sound.opus',
                 'accounts/test-blobs/app.xdc', 'accounts/test-blobs/avatar.png', 'notes.txt', 'key.asc']:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic')
    with tarfile.open(fileobj=io.BytesIO(state_backup.archive(tmp_path)), mode='r:gz') as archive:
        names = archive.getnames()
        assert 'michiflin/accounts/test-blobs/movie.mp4' not in names
        assert 'michiflin/accounts/test-blobs/sound.opus' not in names
        for name in ['accounts/test-blobs/app.xdc', 'accounts/test-blobs/avatar.png', 'notes.txt', 'key.asc']:
            assert 'michiflin/' + name in names


def test_offline_media_removal_never_follows_symlinks_or_removes_originals(tmp_path):
    root = tmp_path / 'state'
    blobs = root / 'accounts' / 'test-blobs'
    blobs.mkdir(parents=True)
    original = tmp_path / 'original.mp4'
    original.write_bytes(b'original')
    (blobs / 'link.mp4').symlink_to(original)
    (blobs / 'local.mp4').write_bytes(b'copy')
    (blobs / 'app.xdc').write_bytes(b'app')
    assert discard_media_files(root) == (1, 4)
    assert original.read_bytes() == b'original'
    assert (blobs / 'app.xdc').read_bytes() == b'app'
    assert not account_blob(original, root)


def test_old_queue_is_held_without_changing_identity_or_replaying_inbox():
    data = {'jobs': {'1': {'status': 'sending', 'inflight': {'msg_id': 42}},
                     '2': {'status': 'queued'}, '3': {'status': 'done'}},
            'apps': {'8': {'build': 'exact'}}, 'notes': {'owner': 'keep'}, 'inbox': {'91': 1}}
    before = copy.deepcopy(data)
    assert hold_old_work(data, 100) == 2
    assert data['jobs']['1']['hold'] and data['jobs']['1']['error'] == 'delivery_unknown'
    assert data['jobs']['1']['inflight'] == before['jobs']['1']['inflight']
    assert data['jobs']['2']['hold']
    assert data['jobs']['3'] == before['jobs']['3']
    assert data['apps'] == before['apps'] and data['notes'] == before['notes']
    assert not data['inbox']
    assert data['flags']['lightweight_recovery']['skip_message_ids'] == ['91']


def test_completed_queue_copies_removed_but_pending_delivery_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'RETAIN_DOWNLOADED_MEDIA', False)
    monkeypatch.setattr(config, 'JOB_ROOT', tmp_path)
    (tmp_path / '1').mkdir()
    (tmp_path / '1' / 'media.mp4').write_bytes(b'copy')
    job = {'id': '1', 'status': 'done', 'messages': [99], 'inflight': {'msg_id': 100}}
    with mock.patch.object(chat, 'discard_delivered_media') as discard:
        queue._discard_completed_download(job)
        assert (tmp_path / '1' / 'media.mp4').exists()
        discard.assert_not_called()
        job['inflight'] = None
        queue._discard_completed_download(job)
        assert not (tmp_path / '1').exists()
        discard.assert_called_once_with([99])


@pytest.mark.parametrize('pending,exists', [(True, True), (False, False)])
def test_delivered_blob_is_not_removed_if_shared_with_pending_send(monkeypatch, tmp_path, pending, exists):
    monkeypatch.setattr(config, 'RETAIN_DOWNLOADED_MEDIA', False)
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    path = tmp_path / 'accounts' / 'test-blobs' / 'video.mp4'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'copy')
    messages = {1: SimpleNamespace(file=str(path), state=26, file_mime='video/mp4'),
                2: SimpleNamespace(file=str(path), state=20 if pending else 26, file_mime='video/mp4')}
    rpc = mock.Mock()
    rpc.get_message.side_effect = lambda account, mid: messages[mid]
    rpc.get_chatlist_entries.return_value = [10]
    rpc.get_message_ids.return_value = [1, 2]
    monkeypatch.setattr(chat, '_bot', SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(chat, '_accid', 1)
    chat.discard_delivered_media([1])
    assert path.exists() is exists
    rpc.delete_messages.assert_not_called()
    rpc.delete_messages_for_all.assert_not_called()


def test_registry_is_fail_closed_and_needs_no_retired_implementation(monkeypatch, tmp_path):
    assert app_compat.pre_refresh_build('test-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES) == '55cacec817'
    assert not app_compat.pre_refresh_build('unknown-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES)
    changed = tmp_path / 'known-builds.json'
    changed.write_bytes(app_compat._KNOWN_BUILDS.read_bytes() + b' ')
    monkeypatch.setattr(app_compat, '_KNOWN_BUILDS', changed)
    assert not app_compat.pre_refresh_build('test-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES)
    assert not app_compat.published_build('test-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES)
    for package in ['e8a104c', 'ec7e67e', 'pre-refresh']:
        assert {p.name for p in (Path(app_compat.__file__).parent / 'compat' / package).iterdir()} == {'manifest.json'}
