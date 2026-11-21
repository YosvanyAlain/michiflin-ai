"""Release storage must not inherit Git's smaller blob limit or drop attachments."""
from unittest import mock

import pytest
import state_backup as backup


@pytest.mark.parametrize('headers,chunks,accepted', [
    ({'Content-Length': '8'}, [b'1234', b'5678'], True),
    ({}, [b'1234', b'5678'], True),
    ({'Content-Length': '9'}, [b'123456789'], False),
    ({}, [b'1234', b'56789'], False),
    ({'Content-Length': '1'}, [b'1234', b'56789'], False),
    ({'Content-Length': '-1'}, [], False),
    ({'Content-Length': 'invalid'}, [], False),
])
def test_release_download_is_bounded_with_or_without_honest_length(monkeypatch, headers, chunks, accepted):
    monkeypatch.setattr(backup, 'MAX_RELEASE_BACKUP_BYTES', 8)
    response = mock.Mock(headers=headers)
    response.iter_content.return_value = iter(chunks)
    with mock.patch.object(backup.requests, 'get', return_value=response) as get:
        if accepted:
            assert backup._download_asset('synthetic', 'owner/private', 123) == b'12345678'
        else:
            with pytest.raises(ValueError):
                backup._download_asset('synthetic', 'owner/private', 123)
        assert get.call_args.kwargs['stream'] is True
    response.close.assert_called_once()


def test_release_download_closes_response_after_network_error():
    response = mock.Mock(headers={})
    response.iter_content.side_effect = backup.requests.Timeout
    with mock.patch.object(backup.requests, 'get', return_value=response):
        with pytest.raises(backup.requests.Timeout):
            backup._download_asset('synthetic', 'owner/private', 123)
    response.close.assert_called_once()


def test_release_size_limit_is_independent_and_still_bounded():
    assert backup.MAX_BACKUP_BYTES == 90_000_000  # Git is unchanged.
    assert 90_000_000 < backup.MAX_RELEASE_BACKUP_BYTES == 256_000_000


def test_oversized_release_is_not_uploaded_pruned_or_marked_ready(monkeypatch):
    monkeypatch.setattr(backup, 'MAX_RELEASE_BACKUP_BYTES', 1)
    with mock.patch.object(backup, '_assert_private'), \
         mock.patch.object(backup, 'archive', return_value=b'synthetic-data'), \
         mock.patch.object(backup, '_ensure_release') as release, \
         mock.patch.object(backup.requests, 'post') as post, \
         mock.patch.object(backup, '_prune') as prune, \
         mock.patch.object(backup, '_mark_ready') as ready:
        with pytest.raises(ValueError, match='previous backups were kept'):
            backup._save_release('synthetic', 'owner/private', 'synthetic-test-key')
    release.assert_not_called()
    post.assert_not_called()
    prune.assert_not_called()
    ready.assert_not_called()
