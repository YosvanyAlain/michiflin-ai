"""Opt-in update keeps the exact preceding package and never sends attachments."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from michiflin import app_compat, config, miniapp, state


def previous():
    return app_compat.community_previous_build(config.BOT_EPOCH, config.MINIAPP_RELEASE, str(miniapp.APP_DIR), miniapp.APP_FILES)


def test_exact_previous_package_remains_usable_even_with_strict_release(monkeypatch):
    # Optional compatibility is a separate mode from the deployed current-only policy.
    monkeypatch.setattr(config, 'MINIAPP_CURRENT_ONLY', False)
    monkeypatch.setattr(config, 'REVOKE_LEGACY_MINIAPPS', True)
    monkeypatch.setattr(config, 'BOT_EPOCH', 'synthetic-epoch')
    monkeypatch.setattr(config, 'MINIAPP_RELEASE', 'synthetic-release')
    old = previous()
    assert len(old) == 10 and old != miniapp.app_build()
    assert miniapp.compatible_build({'epoch': config.BOT_EPOCH, 'build': old})
    assert not miniapp.compatible_build({'epoch': 'foreign', 'build': old})
    assert not miniapp.compatible_build({'epoch': config.BOT_EPOCH, 'build': 'retired0000'})
    # The actual chosen production policy must reject even this exact old build.
    monkeypatch.setattr(config, 'MINIAPP_CURRENT_ONLY', True)
    assert not miniapp.compatible_build({'epoch': config.BOT_EPOCH, 'build': old})
    assert miniapp.compatible_build({'epoch': config.BOT_EPOCH, 'build': miniapp.app_build()})
    monkeypatch.setattr(config, 'MINIAPP_CURRENT_ONLY', False)
    monkeypatch.setattr(config, 'MINIAPP_RELEASE', 'another-release')
    assert not miniapp.compatible_build({'epoch': config.BOT_EPOCH, 'build': old})


def test_archived_live_bytes_match_pinned_community_manifest(tmp_path):
    import hashlib
    import json
    import shutil
    directory = Path('michiflin/compat/community-before')
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert hashlib.sha256((directory / 'live.js').read_bytes()).hexdigest() == manifest['live.js']
    copied = tmp_path / 'community-before'
    shutil.copytree(directory, copied)
    replaced = {'index.html', 'app.css', 'app.js', 'i18n.js', 'core.js', 'books.js', 'live.js'}
    arguments = ('synthetic-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES, copied, replaced, 'synthetic-release')
    assert len(app_compat._uncached_pinned_build(*arguments)) == 10
    (copied / 'live.js').write_text('tampered')
    assert app_compat._uncached_pinned_build(*arguments) == ''  # altered assets fail closed


def test_announce_once_inside_existing_instance_under_concurrent_hello(monkeypatch):
    entry = {'build': 'previous01'}
    monkeypatch.setattr(miniapp, '_apps', lambda: {'123': entry})
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'newbuild01')
    monkeypatch.setattr(miniapp, '_status_allowed', Mock(return_value=True))
    send = Mock(return_value=True)
    monkeypatch.setattr(miniapp, '_send_status', send)
    attachment = Mock(); monkeypatch.setattr(miniapp, 'send_app', attachment)
    monkeypatch.setattr(state, '_save_soon', Mock())
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(miniapp.announce_optional_update, [123] * 8))
    send.assert_called_once()
    assert send.call_args.args[0] == 123
    payload = send.call_args.args[1]
    assert payload['t'] == 'push' and payload['kind'] == 'notice'
    assert 'Tú decides' in payload['data']['es']
    attachment.assert_not_called()
    assert entry['build'] == 'previous01'


def test_revoked_binding_not_notified_and_failed_delivery_can_retry(monkeypatch):
    entry = {'build': 'previous01'}
    monkeypatch.setattr(miniapp, '_apps', lambda: {'123': entry})
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'newbuild01')
    allowed = Mock(return_value=False); monkeypatch.setattr(miniapp, '_status_allowed', allowed)
    send = Mock(return_value=False); monkeypatch.setattr(miniapp, '_send_status', send)
    monkeypatch.setattr(state, '_save_soon', Mock())
    miniapp.announce_optional_update(123); send.assert_not_called()
    allowed.return_value = True
    miniapp.announce_optional_update(123)
    assert not entry.get('optional_update_notice')
    send.return_value = True
    miniapp.announce_optional_update(123)
    assert send.call_count == 2 and entry['optional_update_notice'] == 'newbuild01'


def test_workflow_no_longer_has_automatic_owner_refresh():
    source = Path('.github/workflows/michiflin.yml').read_text()
    assert 'deliver_owner_refresh' not in source
    assert 'OWNER_UI_REFRESH_REVISION' not in source
    assert "cancel-in-progress: false" in source
