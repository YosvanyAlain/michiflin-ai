"""New mobile/library features: fixtures only, no real donated credentials or mail IO."""
import copy
import io
import json
import zipfile
from types import SimpleNamespace
from unittest import mock

import pytest
from michiflin import api_donations, book_library as books, config, group_tools as groups, personal_ai, public_media, state


def test_public_library_sources_and_gutenberg_access_filter(monkeypatch):
    assert {'gutenberg', 'openlibrary', 'google', 'archive', 'standard', 'web'} <= books.SOURCES
    monkeypatch.setattr(books, '_gutenberg', lambda *args: {'results': [
        {'id': 1, 'title': '<b>Only text</b>', 'authors': [{'name': 'Author'}], 'copyright': False,
         'formats': {'text/plain; charset=utf-8': 'https://www.gutenberg.org/1.txt', 'image/jpeg': 'https://www.gutenberg.org/1.jpg'}},
        {'id': 2, 'title': 'Protected', 'copyright': True, 'formats': {}}], 'next': None})
    rows, more = books._search('gutenberg', 'test', 1, '')
    assert len(rows) == 1 and rows[0]['downloads'][0]['format'] == 'txt'
    assert not more


@pytest.mark.parametrize('source,page', [('missing', 1), ('all', True), ('web', -1), ('google', 101)])
def test_invalid_catalogue_queries_never_fetch(monkeypatch, source, page):
    fetch = mock.Mock()
    monkeypatch.setattr(books, '_fetch', fetch)
    with pytest.raises(books.BookError): books.search(source, 'query', page)
    fetch.assert_not_called()


def test_archive_never_offers_restricted_loan(monkeypatch):
    monkeypatch.setattr(books, '_json', lambda url: {'metadata': {'access-restricted-item': 'true'}, 'files': [{'name': 'book.epub'}]})
    assert books.archive_download('fixture-book') == []


def test_full_epub_reading_order_and_script_removal(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('META-INF/container.xml', '<container><rootfile full-path="OEBPS/book.opf"/></container>')
        archive.writestr('OEBPS/book.opf', '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/></manifest><spine><itemref idref="b"/><itemref idref="a"/></spine></package>')
        archive.writestr('OEBPS/a.xhtml', '<p>Last chapter</p><script>stealSecret()</script>')
        archive.writestr('OEBPS/b.xhtml', '<p>First chapter</p>')
    monkeypatch.setattr(books, '_fetch', lambda *args: (buffer.getvalue(), 'application/epub+zip'))
    result = books.reading_copy('https://example.org/book.epub', 'epub')
    assert result['complete'] and result['text'].index('First') < result['text'].index('Last')
    assert 'stealSecret' not in result['text']


def test_books_never_silently_truncate_or_treat_login_html_as_text(monkeypatch):
    monkeypatch.setattr(books, '_fetch', lambda *args: (b'<html>Login required</html>', 'text/html'))
    with pytest.raises(books.BookError): books.reading_copy('https://example.org/book.txt', 'txt')
    monkeypatch.setattr(books, '_fetch', lambda *args: (b'123456789', 'text/plain'))
    monkeypatch.setattr(books, 'MAX_TEXT', 4)
    with pytest.raises(books.BookError): books.reading_copy('https://example.org/book.txt', 'txt')


@pytest.mark.parametrize('url', ['http://example.org/book.txt', 'https://user:password@example.org/book.txt', 'https://example.org/?api_key=x'])
def test_library_rejects_private_credential_urls(url):
    with pytest.raises(books.BookError): books._fetch(url)


def test_thumbnails_only_construct_official_fixed_host(monkeypatch):
    original = public_media.image_data
    image = mock.Mock(return_value='data:image/jpeg;base64,AA==')
    monkeypatch.setattr(public_media, 'image_data', image)
    result = public_media.youtube_thumbnails(['abcdefghijk', 'abcdefghijk'], 'high')
    assert len(result['images']) == 1
    image.assert_called_once_with('https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg', 'high')
    with pytest.raises(ValueError): public_media.youtube_thumbnails([['bad']], 'low')
    with pytest.raises(ValueError): public_media.youtube_thumbnails(['../../secret'], 'low')
    with pytest.raises(ValueError): original('https://127.0.0.1/image.jpg')


def test_group_commands_are_small_and_have_no_private_functions():
    assert groups.parse('/tasas') == ('rates', '')
    assert groups.parse('/michiflin activar') == ('enable', '')
    assert groups.parse('/buscar clima tropical') == ('search', 'clima tropical')
    for command in ['/app', '/imagen a cat', '/code print(1)', '/personal_ai', '/api key', 'hello']:
        assert groups.parse(command) is None


def test_group_search_never_calls_ai_or_personal_provider(monkeypatch):
    monkeypatch.setattr(groups.tools, 'search', mock.Mock(return_value=[SimpleNamespace(title='Source', snippet='Public text', url='https://example.org')]))
    from michiflin import ai
    with mock.patch.object(ai, 'complete', side_effect=AssertionError('AI forbidden')):
        reply = groups.public_answer('search', 'query')
    assert 'sin IA' in reply and 'https://example.org' in reply
    groups.tools.search.assert_called_once_with('query', 'es', limit=4, engine='standard')


def test_group_owner_must_match_previously_bound_key(monkeypatch):
    monkeypatch.setattr(config, 'OWNER_ADDRESS', 'owner@example.invalid')
    monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', True)
    fingerprint = 'A' * 40
    monkeypatch.setattr(groups.identity_guard, 'contact_fingerprint', lambda *args: fingerprint)
    monkeypatch.setitem(state.data, 'email_key_bindings', {'owner@example.invalid': {'key': groups.identity_guard._key_digest(fingerprint)}})
    rpc = mock.Mock()
    rpc.get_contact.return_value = SimpleNamespace(address='owner@example.invalid', is_key_contact=True, e2ee_avail=True)
    assert groups.owner_verified(rpc, 1, 42)
    rpc.get_contact.return_value.is_key_contact = False
    assert not groups.owner_verified(rpc, 1, 42)


@pytest.mark.parametrize('consent,version', [(False, api_donations.CONSENT_VERSION), (True, 'old'), (1, api_donations.CONSENT_VERSION)])
def test_donation_requires_explicit_current_consent_without_network(monkeypatch, consent, version):
    request = mock.Mock()
    monkeypatch.setattr(api_donations, '_request', request)
    with pytest.raises(personal_ai.PersonalError):
        api_donations.donate('donor@example.invalid', 'personal-test', consent=consent, version=version, daily=20)
    request.assert_not_called()


@pytest.fixture
def donation(monkeypatch):
    monkeypatch.setenv('WEB_SESSION_KEY', 's' * 64)
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-token')
    monkeypatch.setenv('STATE_BACKUP_REPO', 'owner/private')
    provider = {'id': 'personal-test', 'name': 'Fixture', 'kind': 'openai', 'base': 'https://api.example.org/v1', 'key': 'NEVER-PUBLISH-THIS-KEY', 'models': []}
    vault = {'version': 1, 'providers': [provider], 'owner': 'donor@example.invalid'}
    monkeypatch.setattr(personal_ai, '_load', lambda address: copy.deepcopy(vault))
    def save(address, value):
        vault.clear(); vault.update(copy.deepcopy(value))
    monkeypatch.setattr(personal_ai, '_save', save)
    uploaded = {}
    private = [True]
    def request(method, url, token, **kwargs):
        if url.endswith('/owner/private'): return SimpleNamespace(status_code=200, json=lambda: {'private': private[0], 'archived': False})
        if '/releases/tags/' in url: return SimpleNamespace(status_code=200, json=lambda: {'id': 7})
        if method == 'GET' and '/assets?' in url:
            return SimpleNamespace(status_code=200, json=lambda: [{'id': 8, 'name': uploaded['name']}] if uploaded else [])
        if method == 'POST' and '/assets' in url:
            uploaded.update(blob=kwargs['data'], name=kwargs['params']['name'])
            return SimpleNamespace(status_code=201, json=lambda: {'id': 8})
        if method == 'DELETE': return SimpleNamespace(status_code=204)
        raise AssertionError(method + url)
    monkeypatch.setattr(api_donations, '_request', request)
    response = mock.Mock()
    response.iter_content.side_effect = lambda size: iter([uploaded['blob']])
    monkeypatch.setattr(api_donations.requests, 'get', lambda *args, **kwargs: response)
    return SimpleNamespace(vault=vault, uploaded=uploaded, private=private, provider=provider)


def test_donation_is_encrypted_verified_idempotent_and_not_activated(donation):
    result = api_donations.donate('donor@example.invalid', 'personal-test', consent=True, version=api_donations.CONSENT_VERSION, daily=20)
    assert result['accepted'] and result['active'] is False
    assert b'NEVER-PUBLISH-THIS-KEY' not in donation.uploaded['blob']
    decoded = json.loads(personal_ai._fernet(b'api-donation').decrypt(donation.uploaded['blob']))
    assert decoded['provider']['key'] == donation.provider['key'] and decoded['consent']['daily'] == 20
    before = donation.uploaded['blob']
    api_donations.donate('donor@example.invalid', 'personal-test', consent=True, version=api_donations.CONSENT_VERSION, daily=20)
    assert donation.uploaded['blob'] == before
    assert 'NEVER-PUBLISH' not in json.dumps(result)
    assert api_donations.revoke('donor@example.invalid', 'personal-test')['donations'][0]['state'] == 'revoked'


def test_donation_rejects_public_repository(donation):
    donation.private[0] = False
    with pytest.raises(personal_ai.PersonalError, match='donation_private_repo'):
        api_donations.donate('donor@example.invalid', 'personal-test', consent=True, version=api_donations.CONSENT_VERSION, daily=20)
    assert not donation.uploaded


@pytest.mark.parametrize('kind,url,expected', [('openai', 'https://api.example.org/v1/chat/completions', 'https://api.example.org/v1'),
    ('anthropic', 'https://api.anthropic.com/v1/messages', 'https://api.anthropic.com/v1'),
    ('gemini', 'https://generativelanguage.googleapis.com/v1beta/models', 'https://generativelanguage.googleapis.com/v1beta')])
def test_personal_setup_accepts_copied_api_endpoint(kind, url, expected):
    assert personal_ai._normalize_base(url, kind) == expected


def test_group_activation_requires_real_owner_and_membership_stays_encrypted(monkeypatch):
    monkeypatch.setenv('GROUP_PUBLIC_TOOLS', '1')
    monkeypatch.setitem(state.data, 'public_groups', {})
    monkeypatch.setattr(state, 'save', mock.Mock())
    monkeypatch.setattr(groups, 'owner_verified', mock.Mock(return_value=False))
    rpc = mock.Mock()
    rpc.send_msg.return_value = 101
    monkeypatch.setattr(groups.chat, '_recent_out', [])
    bot = SimpleNamespace(rpc=rpc)
    info = SimpleNamespace(is_encrypted=True, chat_type='Group')
    message = SimpleNamespace(text='/michiflin activar', file=None, chat_id=42, from_id=9, show_padlock=True)
    assert not groups.offer(bot, 1, message, info)
    rpc.send_msg.assert_not_called()
    assert not state.data['public_groups']
    groups.owner_verified.return_value = True
    assert groups.offer(bot, 1, message, info)
    assert state.data['public_groups']['42']['enabled'] is True
    rpc.send_msg.assert_called_once()
    monkeypatch.setattr(groups, 'owner_present', lambda *args: True)
    rpc.get_basic_chat_info.return_value = info
    assert groups._enabled(rpc, 1, 42)
    info.is_encrypted = False
    assert not groups._enabled(rpc, 1, 42)


def test_incomplete_reasoning_is_not_leaked_as_answer_or_code():
    from michiflin import util, config, coder, smart_agent
    assert util.strip_thinking('<think>unfinished internal analysis') == ''
    assert util.strip_thinking('<think>complete thought</think>Final') == 'Final'
    assert util.strip_thinking('Ordinary final answer') == 'Ordinary final answer'
    assert 'Michiflin' in config.AI_SYSTEM_PROMPT and 'black' in config.AI_SYSTEM_PROMPT
    assert 'Michiflin Coder' in coder.system_prompt('es', False)
    assert 'Michiflin' in smart_agent._prompt('es')


def test_book_pdf_parser_has_no_bot_secrets_and_is_time_memory_limited(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'NEVER-INHERIT-THIS')
    monkeypatch.setattr(books, '_fetch', lambda *args: (b'%PDF-1.7 fixture', 'application/pdf'))
    run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=b'Extracted complete reading text'))
    monkeypatch.setattr(books.subprocess, 'run', run)
    assert books.reading_copy('https://example.org/book.pdf', 'pdf')['complete']
    assert 'GITHUB_TOKEN' not in run.call_args.kwargs['env']
    assert run.call_args.kwargs['timeout'] == 18
    script = run.call_args.args[0][-1]
    assert 'RLIMIT_CPU' in script and 'RLIMIT_AS' in script


def test_old_experimental_command_is_not_registered():
    from michiflin import commands
    assert 'ping' not in commands.COMMANDS
    assert commands.parse('/status') is not None


def test_plaintext_group_message_cannot_activate_or_trigger_tools(monkeypatch):
    monkeypatch.setenv('GROUP_PUBLIC_TOOLS', '1')
    verified = mock.Mock(return_value=True)
    monkeypatch.setattr(groups, 'owner_verified', verified)
    rpc = mock.Mock()
    message = SimpleNamespace(text='/michiflin activar', file=None, chat_id=42, from_id=9, show_padlock=False)
    info = SimpleNamespace(is_encrypted=True, chat_type='Group', is_contact_request=True)
    assert not groups.offer(SimpleNamespace(rpc=rpc), 1, message, info)
    verified.assert_not_called()
    rpc.accept_chat.assert_not_called()
    rpc.send_msg.assert_not_called()


def test_pdf_reading_copy_extracts_all_pages_with_actual_bounded_child(monkeypatch):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    for text in ['First page', 'Last page']:
        page = writer.add_blank_page(300, 300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 12 Tf 20 250 Td (' + text + ') Tj ET').encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    raw = io.BytesIO(); writer.write(raw)
    monkeypatch.setattr(books, '_fetch', lambda *args: (raw.getvalue(), 'application/pdf'))
    result = books.reading_copy('https://example.org/book.pdf', 'pdf')
    assert result['complete'] and 'First page' in result['text'] and 'Last page' in result['text']
    assert '\\n' not in result['text']


def test_epub_missing_spine_cannot_be_reported_as_complete(monkeypatch):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('META-INF/container.xml', '<container><rootfile full-path="book.opf"/></container>')
        archive.writestr('book.opf', '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="a" href="ok.xhtml"/></manifest><spine><itemref idref="a"/><itemref idref="missing"/></spine></package>')
        archive.writestr('ok.xhtml', '<p>Only one chapter</p>')
    monkeypatch.setattr(books, '_fetch', lambda *args: (data.getvalue(), 'application/epub+zip'))
    with pytest.raises(books.BookError, match='epub_structure'):
        books.reading_copy('https://example.org/book.epub', 'epub')


def test_group_weather_never_uses_personal_home_or_saves_a_user_city(monkeypatch):
    from michiflin import weather_home
    private = mock.Mock(side_effect=AssertionError('personal home must not be used'))
    monkeypatch.setattr(weather_home, 'query', private)
    monkeypatch.setattr(groups.tools, 'weather_data', lambda city, locale: {
        'place': {'label': city}, 'current': {'description': 'Despejado', 'temp': 28, 'humidity': 70, 'wind': 12},
        'days': [{'date': '2026-11-20', 'description': 'Sol', 'low': 20, 'high': 30}], 'as_of': 'fixture'})
    result = groups.public_answer('weather', 'Santiago, Cuba')
    assert 'Santiago, Cuba' in result and 'Open-Meteo' in result
    assert 'Inicio' not in result
    private.assert_not_called()


def test_group_rates_never_present_saved_data_as_a_fresh_quote(monkeypatch):
    monkeypatch.setattr(groups.tools, 'rates_snapshot', lambda: {'rates': {'USD': 325, 'EUR': 350}, 'stale': True, 'at': 100})
    assert 'Última lectura guardada' in groups.public_answer('rates', '')


def test_cover_redirects_only_to_known_public_image_hosts(monkeypatch):
    from PIL import Image
    buffer = io.BytesIO(); Image.new('RGB', (80, 120), '#102127').save(buffer, 'JPEG')
    first = mock.Mock(status_code=302, headers={'Location': 'https://archive.org/download/public/cover.jpg'})
    final = mock.Mock(status_code=200, headers={})
    request = mock.Mock(side_effect=[first, final])
    monkeypatch.setattr(public_media.net, 'request', request)
    monkeypatch.setattr(public_media.net, 'read_limited', lambda *args: buffer.getvalue())
    result = public_media.image_data('https://covers.openlibrary.org/b/id/777-M.jpg', cover=True)
    assert result.startswith('data:image/jpeg;base64,') and request.call_count == 2
    first.close.assert_called_once(); final.close.assert_called_once()
    malicious = mock.Mock(status_code=302, headers={'Location': 'https://127.0.0.1/private'})
    request = mock.Mock(return_value=malicious)
    monkeypatch.setattr(public_media.net, 'request', request)
    with pytest.raises(ValueError):
        public_media.image_data('https://covers.openlibrary.org/b/id/778-M.jpg', cover=True)
    assert request.call_count == 1
    malicious.close.assert_called_once()
