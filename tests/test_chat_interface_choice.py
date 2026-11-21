"""Explicit private-chat choice, no billing/replay before opt-in; fake accounts."""
import copy
import json
import re
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from michiflin import access, admin, app, chat, commands, config, interaction, miniapp, progress, state

ALICE = 'alice@example.invalid'
BOB = 'bob@example.invalid'
OWNER = 'owner@example.invalid'


@pytest.fixture
def visitor(monkeypatch):
    original = copy.deepcopy(state.data)
    sender = chat.sender()
    for key in ['settings', 'prefs', 'chat_interfaces', 'kill_switch', 'jobs', 'apps']:
        monkeypatch.setitem(state.data, key, {})
    monkeypatch.setattr(config, 'OWNER_ADDRESS', OWNER)
    monkeypatch.setattr(config, 'ADMIN_ADDRESSES', {OWNER})
    monkeypatch.setattr(config, 'BOT_EPOCH', 'synthetic-epoch')
    monkeypatch.setattr(state, 'save', Mock())
    monkeypatch.setattr(state, 'work_interrupted', lambda *a: False)
    monkeypatch.setattr(state, 'guard_work', lambda *a: nullcontext())
    monkeypatch.setattr(access, 'session_stamp', lambda address: 'grant-' + address)
    monkeypatch.setattr(admin, 'is_admin', lambda address: address == OWNER)
    monkeypatch.setattr(admin, 'is_allowed', lambda address: True)
    monkeypatch.setattr(chat, 'recipient_guard', lambda *a: nullcontext())
    monkeypatch.setattr(chat, 'recipient_allowed', lambda *a: True)
    monkeypatch.setattr(progress, 'begin', Mock())
    monkeypatch.setattr(progress, 'end', Mock())
    monkeypatch.setattr(chat, 'text', text := Mock(return_value=100))
    monkeypatch.setattr(chat, 'delete', Mock())
    monkeypatch.setattr(app.agent, 'handle', Mock())
    monkeypatch.setattr(app.agent, 'handle_attachment', Mock())
    monkeypatch.setattr(miniapp, 'send_app', Mock(return_value=101))
    monkeypatch.setattr(app, '_last_locale', {})
    chat.set_sender(ALICE)
    yield text
    state.data.clear(); state.data.update(original)
    chat.set_sender(sender)


def process(text, *, address=ALICE, chat_id=21, file=None, locale='es'):
    m = SimpleNamespace(id=981101, chat_id=chat_id, from_id=15, file=file)
    app._process(m, text, locale, address)


@pytest.mark.parametrize('locale,text,word,other', [('es', 'hola', '¿Cómo prefieres', 'How would'),
                                                   ('en', 'hello', 'How would', '¿Cómo prefieres')])
def test_first_text_only_explains_no_ai_or_implicit_replay(visitor, locale, text, word, other):
    process(text, locale=locale)
    reply = visitor.call_args.args[1]
    assert word in reply and other not in reply
    assert '/app' in reply and ('/commands' if locale == 'en' else '/comandos') in reply
    app.agent.handle.assert_not_called()
    app.agent.handle_attachment.assert_not_called()
    assert interaction.record(21)['prompted'] and not interaction.record(21)['mode']
    process('/comandos', locale='es')
    assert interaction.record(21)['mode'] == 'full'
    app.agent.handle.assert_not_called()  # original "hola" never gets replayed
    assert sum(word in call.args[1] for call in visitor.call_args_list) == 1


def test_first_command_and_attachment_never_execute_or_delete_before_choice(visitor, monkeypatch):
    operation = Mock()
    monkeypatch.setitem(commands.COMMANDS, 'calc', operation)
    process('/calc 2+2')
    process('analiza esta foto', file='/synthetic/photo.jpg')
    operation.assert_not_called()
    app.agent.handle_attachment.assert_not_called()
    chat.delete.assert_not_called()
    assert 'Elige /app o /comandos' in visitor.call_args.args[1]


def test_commands_explicitly_enable_real_calculator_without_miniapp(visitor):
    process('/commands', locale='en')
    assert interaction.record(21)['mode'] == 'full'
    assert state.setting('chat_mode') == 'full'
    assert 'Available command features' in visitor.call_args.args[1]
    assert 'library, Live and local editors require the miniapp' in visitor.call_args.args[1]
    process('/calc 2+2', locale='en')
    assert '4' in visitor.call_args.args[1]
    miniapp.send_app.assert_not_called()
    app.agent.handle.assert_not_called()


def test_choice_persists_without_repeated_menu_and_can_change(visitor):
    process('/comandos')
    state.data['chat_interfaces'] = json.loads(json.dumps(state.data['chat_interfaces']))
    visitor.reset_mock()
    process('hola de nuevo')
    app.agent.handle.assert_called_once()
    visitor.assert_not_called()
    process('/modo app')
    assert interaction.record(21)['mode'] == 'app'
    miniapp.send_app.assert_called_once()
    process('no quiero ejecutar esto')
    assert '/comandos' in visitor.call_args.args[1]
    assert app.agent.handle.call_count == 1
    process('/mode chat', locale='en')
    assert interaction.record(21)['mode'] == 'full'


def test_preferences_never_admit_another_chat_person_grant_or_epoch(visitor, monkeypatch):
    process('/comandos')
    assert interaction.record(22) == {}
    chat.set_sender(BOB)
    state.data['settings']['chat_mode'] = 'full'  # owner's default isn't consent
    assert interaction.record(21) == {}
    process('hello', address=BOB, chat_id=22, locale='en')
    assert interaction.record(22)['mode'] == ''
    chat.set_sender(ALICE)
    monkeypatch.setattr(access, 'session_stamp', lambda a: 'new-grant')
    assert interaction.record(21) == {}
    monkeypatch.setattr(config, 'BOT_EPOCH', 'another-epoch')
    assert interaction.record(21) == {}


def test_unverified_recipient_is_rejected_before_any_mode_prompt(visitor, monkeypatch):
    monkeypatch.setattr(chat, 'recipient_allowed', lambda c: False)
    process('hello')
    visitor.assert_not_called()
    app.agent.handle.assert_not_called()
    assert not state.data['chat_interfaces']


def test_status_control_and_mode_remain_available_during_stop(visitor, monkeypatch):
    monkeypatch.setattr(state, 'work_interrupted', lambda *a: True)
    status = Mock()
    monkeypatch.setitem(commands.COMMANDS, 'estado', status)
    process('/status', locale='en')
    status.assert_called_once()
    process('/commands', locale='en')
    assert interaction.record(21)['mode'] == 'full'
    app.agent.handle.assert_not_called()


def test_explicit_app_settings_keep_mode_in_sync(visitor):
    miniapp.op_settings(21, {'set': {'chat_mode': 'full'}}, 'en')
    assert interaction.record(21)['mode'] == 'full'
    commands.cmd_settings(21, ['chat', 'app'], '', 'es')
    assert interaction.record(21)['mode'] == 'app'
    process('/commands', locale='en')
    miniapp.op_settings(21, {'reset': True}, 'en')
    assert interaction.record(21)['mode'] == state.setting('chat_mode') == 'app'


def test_new_guest_reply_language_does_not_inherit_owner_setting(visitor):
    state.data['settings']['language'] = 'es'
    assert app._locale('hello please', 21) == 'en'
    assert app._locale('/comandos', 21) == 'es'
    assert app._locale('/commands', 21) == 'en'
    state.data['prefs'][ALICE] = {'language': 'es'}
    assert app._locale('hello please', 21) == 'es'


def test_failed_prompt_delivery_or_state_save_does_not_commit_a_choice(visitor, monkeypatch):
    visitor.return_value = 0
    assert interaction.guard(21, 'en')
    assert interaction.record(21) == {}
    visitor.return_value = 1
    monkeypatch.setattr(state, 'save', Mock(side_effect=OSError('synthetic')))
    with pytest.raises(OSError):
        interaction.choose(21, 'full')
    assert interaction.record(21) == {}


def test_chat_only_choice_suppresses_bootstrap_attachment(visitor, monkeypatch):
    process('/commands', locale='en')
    miniapp.welcome(21, '')
    miniapp.send_app.assert_not_called()


@pytest.mark.parametrize('system,pref,expected', [('es-CU', 'auto', 'es'), ('en-US', 'auto', 'en'),
                                                ('fr-FR', 'auto', 'en'), ('es-ES', 'en', 'en'),
                                                ('en-GB', 'es', 'es')])
def test_original_package_uses_system_language_and_preserves_explicit_choice(system, pref, expected):
    source = (Path(__file__).resolve().parents[1] / 'webxdc/core.js').read_text()
    functions = '\n'.join(re.search(r'function ' + name + r'\(\) \{.*?\n\}', source, re.S).group()
                          for name in ['systemLang', 'lang'])
    script = 'const navigator=' + json.dumps({'languages': [system], 'language': system}) + ';const prefs=' + json.dumps({'lang': pref}) + ';' + functions + ';console.log(lang());'
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == expected


def test_two_private_chats_keep_independent_choices_for_same_sender(visitor):
    process('/commands', chat_id=21)
    process('/app', chat_id=22)
    assert interaction.record(21)['mode'] == 'full'
    assert interaction.record(22)['mode'] == 'app'
    state.forget_person(ALICE)
    assert interaction.record(21) == interaction.record(22) == {}


def test_opening_settings_does_not_turn_owner_language_into_guest_preference(visitor):
    state.data['settings'].update(language='es', chat_mode='full')
    miniapp.settings_view('en')
    assert state.data['prefs'][ALICE]['language'] == 'auto'
    assert app._locale('hello please', 21) == 'en'
    assert state.setting('chat_mode') == 'app'
    assert interaction.record(21) == {}
    state.set_setting('language', 'es')
    miniapp.settings_view('en')
    assert app._locale('hello please', 21) == 'es'
