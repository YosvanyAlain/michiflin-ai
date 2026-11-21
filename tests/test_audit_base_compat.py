"""Local QA fixes must not strand the app that users already received."""
from __future__ import annotations

import shutil

from michiflin import app_compat, config, miniapp


def test_pre_audit_build_is_exact_and_epoch_bound(monkeypatch):
    monkeypatch.setattr(config, 'BOT_EPOCH', 'test-epoch')
    old = app_compat.audit_base_build('test-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES)
    assert old == 'c8a5f423ba'
    assert old != miniapp.app_build()
    assert miniapp.compatible_build({'build': old})
    assert miniapp.discloses_infobars({'build': old})
    for build in (app_compat.previous_build, app_compat.recent_build,
                  app_compat.latest_build, app_compat.published_build):
        assert miniapp.compatible_build({'build': build('test-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES)})
    monkeypatch.setattr(config, 'BOT_EPOCH', 'other-epoch')
    assert not miniapp.compatible_build({'build': old})
    assert not miniapp.discloses_infobars({'build': old})


def test_pre_audit_archive_cannot_be_silently_modified(monkeypatch, tmp_path):
    archive = tmp_path / 'archive'
    shutil.copytree(app_compat._AUDIT_BASE, archive)
    monkeypatch.setattr(app_compat, '_AUDIT_BASE', archive)
    monkeypatch.setattr(config, 'BOT_EPOCH', 'test-epoch')
    assert app_compat.audit_base_build('test-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES) == 'c8a5f423ba'
    with (archive / 'manifest.json').open('a') as handle:
        handle.write('\n// tampering\n')
    assert app_compat.audit_base_build('test-epoch', str(miniapp.APP_DIR), miniapp.APP_FILES) == ''
    assert not miniapp.compatible_build({'build': 'c8a5f423ba'})
