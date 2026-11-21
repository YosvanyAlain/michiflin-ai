"""Silent attachment delivery; explicit updates keep their notices (synthetic)."""
from unittest.mock import Mock

import pytest

from michiflin import chat, config, miniapp, state


@pytest.mark.parametrize('locale', ['es', 'en', ''])
@pytest.mark.parametrize('updated', [False, True])
def test_short_welcome_reserves_retirement_notice_for_updates(monkeypatch, locale, updated):
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: True)
    monkeypatch.setattr(miniapp, 'build', lambda: '/synthetic/app.xdc')
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'unchanged-build')
    monkeypatch.setattr(miniapp.access, 'session_stamp', lambda address: 'synthetic-session')
    monkeypatch.setattr(miniapp.maintenance, 'text', lambda language: '')
    monkeypatch.setattr(config, 'REVOKE_LEGACY_MINIAPPS', True)
    monkeypatch.setattr(chat, 'sender', lambda: 'visitor@example.invalid')
    monkeypatch.setattr(chat, 'send', send := Mock(return_value=101))
    monkeypatch.setattr(state, 'save', Mock())
    monkeypatch.setitem(state.data, 'apps', {})
    assert miniapp.send_app(21, locale, updated=updated) == 101
    text = send.call_args.args[1]
    english = locale == 'en'
    assert ('Previous miniapps' in text or 'Las miniapps anteriores' in text) == updated
    if not updated:
        assert text == ""
    assert send.call_count == 1 and send.call_args.kwargs['viewtype'] == 'Webxdc'
    assert state.data['apps']['101']['locale'] == ('en' if english else 'es')
