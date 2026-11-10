"""The currently published WebXDC build remains admitted without changing it."""
from __future__ import annotations

import shutil

from michiflin import app_compat, config, miniapp


def test_e8a104c_published_package_is_exact_and_epoch_bound(monkeypatch):
    monkeypatch.setattr(config, 'BOT_EPOCH', 'test-epoch')
    old = app_compat.published_build('test-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES)
    assert old == '4ac9c2697e'
    assert miniapp.app_build() != old  # a corrected frontend is a new package
    assert miniapp.compatible_build({'build': old})
    assert miniapp.discloses_infobars({'build': old})
    assert all(miniapp.compatible_build({'build': build('test-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES)})
               for build in (app_compat.latest_build, app_compat.recent_build, app_compat.previous_build))
    monkeypatch.setattr(config, 'BOT_EPOCH', 'other-epoch')
    assert not miniapp.compatible_build({'build': old})
    assert not miniapp.discloses_infobars({'build': old})


def test_published_package_is_rejected_if_archived_bytes_change(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'BOT_EPOCH', 'test-epoch')
    archive = tmp_path / 'published'
    shutil.copytree(app_compat._PUBLISHED, archive)
    monkeypatch.setattr(app_compat, '_PUBLISHED', archive)
    assert app_compat.published_build('test-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES) == '4ac9c2697e'
    with (archive / 'app.js').open('a', encoding='utf-8') as handle:
        handle.write('\n// synthetic tampering\n')
    assert app_compat.published_build('test-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES) == ''
    assert not miniapp.compatible_build({'build': '4ac9c2697e'})
    assert not miniapp.discloses_infobars({'build': '4ac9c2697e'})
