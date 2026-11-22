"""The primary backup must ignore legacy Drive settings and never mirror state.

All credentials, paths and network responses in this test are synthetic.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import state_backup as backup


@pytest.mark.parametrize('rotate', [False, True])
def test_primary_backup_ignores_legacy_drive_configuration(monkeypatch, rotate):
    # Even stale deployment settings must not reactivate the removed feature.
    monkeypatch.setenv('DRIVE_BACKUP_ENABLED', '1')
    for name in ('GOOGLE_DRIVE_CLIENT_ID', 'GOOGLE_DRIVE_CLIENT_SECRET',
                 'GOOGLE_DRIVE_REFRESH_TOKEN', 'GOOGLE_DRIVE_FOLDER_ID'):
        monkeypatch.setenv(name, 'synthetic-only')
    monkeypatch.delenv('STATE_BACKUP_NAMESPACE', raising=False)
    forbidden = Mock(side_effect=AssertionError('removed Drive mirror was invoked'))
    monkeypatch.setitem(sys.modules, 'drive_backup', SimpleNamespace(enabled=forbidden, mirror=forbidden))

    uploaded = {}
    def upload(url, **kwargs):
        assert url == 'https://uploads.github.com/repos/owner/private/releases/7/assets'
        uploaded['bytes'] = kwargs['data']
        assert uploaded['bytes'].startswith(backup.MAGIC)
        assert b'synthetic complete archive' not in uploaded['bytes']
        return Mock(json=lambda: {'id': 123})

    def rename(url, **kwargs):
        assert url == 'https://api.github.com/repos/owner/private/releases/assets/123'
        return Mock(json=lambda: {'id': 123, 'name': kwargs['json']['name'], 'state': 'uploaded'})

    monkeypatch.setattr(backup, '_assert_private', Mock())
    monkeypatch.setattr(backup, 'archive', Mock(return_value=b'synthetic complete archive'))
    monkeypatch.setattr(backup, '_ensure_release', Mock(return_value={'id': 7}))
    monkeypatch.setattr(backup.requests, 'post', Mock(side_effect=upload))
    monkeypatch.setattr(backup.requests, 'patch', Mock(side_effect=rename))
    monkeypatch.setattr(backup.requests, 'get', forbidden)
    monkeypatch.setattr(backup.requests, 'delete', forbidden)
    monkeypatch.setattr(backup, '_download_asset', lambda *args: uploaded['bytes'])
    verify = Mock(); monkeypatch.setattr(backup, '_verify_plaintext', verify)
    ready = Mock(); monkeypatch.setattr(backup, '_mark_ready', ready)
    prune = Mock(); monkeypatch.setattr(backup, '_prune', prune)

    backup._save_release('synthetic-token', 'owner/private', 'synthetic passphrase', rotate=rotate)

    verify.assert_called_once_with(b'synthetic complete archive')
    ready.assert_called_once()
    assert backup.requests.post.call_count == backup.requests.patch.call_count == 1
    assert prune.call_count == int(rotate)
    forbidden.assert_not_called()


def test_workflow_does_not_expose_removed_backup_controls():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / '.github/workflows/michiflin.yml').read_text(encoding='utf-8')
    assert 'DRIVE_BACKUP_ENABLED' not in workflow
    assert 'GOOGLE_DRIVE_' not in workflow
    assert 'STATE_BACKUP_RETAIN_ALL: "1"' in workflow
    assert 'STATE_BACKUP_REQUIRED: "1"' in workflow
    assert not (root / 'drive_backup.py').exists()
