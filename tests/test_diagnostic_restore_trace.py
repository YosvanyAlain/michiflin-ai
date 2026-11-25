"""Offline proof that diagnostic restore records the actual ciphertext only."""
from __future__ import annotations

import hashlib
import json

import pytest

from tools import diagnostic_restore_trace as trace


def test_single_actual_download_is_recorded_without_credentials(monkeypatch, tmp_path):
    blob = trace.state_backup.MAGIC + b'synthetic encrypted bytes'
    def download(token, repo, asset_id):
        assert (token, repo, asset_id) == ('private-token', 'private-repo', 123)
        return blob
    monkeypatch.setattr(trace.state_backup, '_download_asset', download)
    monkeypatch.setattr(trace.state_backup, 'restore',
                        lambda: trace.state_backup._download_asset('private-token', 'private-repo', 123))
    result = trace.restore_traced()
    assert result == {'schema': 1, 'read_only': True, 'mail_io_started': False, 'asset_id': 123,
                      'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
    assert trace.state_backup._download_asset is download
    output = tmp_path / 'trace.json'
    trace.write_private(output, result)
    assert json.loads(output.read_text()) == result
    assert output.stat().st_mode & 0o777 == 0o600
    assert 'private-token' not in output.read_text() and 'private-repo' not in output.read_text()
    with pytest.raises(FileExistsError):
        trace.write_private(output, result)


@pytest.mark.parametrize('count', [0, 2])
def test_ambiguous_restores_fail_without_guessing(monkeypatch, count):
    def download(_token, _repo, _asset_id):
        return trace.state_backup.MAGIC + b'synthetic'
    monkeypatch.setattr(trace.state_backup, '_download_asset', download)
    def restore():
        for asset in range(1, count + 1):
            trace.state_backup._download_asset('t', 'r', asset)
    monkeypatch.setattr(trace.state_backup, 'restore', restore)
    with pytest.raises(RuntimeError, match='ambiguous_restored_asset'):
        trace.restore_traced()
    assert trace.state_backup._download_asset is download


def test_failed_restore_cannot_publish_trace_and_restores_function(monkeypatch):
    def download(*_args):
        return trace.state_backup.MAGIC + b'synthetic'
    monkeypatch.setattr(trace.state_backup, '_download_asset', download)
    def fail():
        trace.state_backup._download_asset('t', 'r', 9)
        raise ValueError('private error')
    monkeypatch.setattr(trace.state_backup, 'restore', fail)
    with pytest.raises(ValueError):
        trace.restore_traced()
    assert trace.state_backup._download_asset is download
