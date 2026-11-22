"""Official connections: explicit UI, private-chat gate and synthetic provider flows.

No real provider account, credential, bot, or network API is used in these tests.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from itertools import product
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from michiflin import access, admin, chat, community, config, identity_guard, live_web, miniapp, perf, state, usage
from michiflin import facebook_account as fb, tiktok_account as tt, youtube_account as yt
from tests.test_webxdc_browser import browser_fixture

OWNER = "owner@example.org"


def _open_connections(page):
    page.evaluate("goTab('settings')")
    page.locator('[data-setting-section="privacy"]').click()
    page.get_by_role('button', name='Conexiones oficiales' if page.evaluate("lang()") == 'es' else 'Official connections', exact=True).click()


@pytest.mark.parametrize('height', [568, 640])
@pytest.mark.parametrize('engine,locale', [('chromium', 'es'), ('webkit', 'en')])
def test_ui_flows_are_explicit_bilingual_and_render_untrusted_results_as_text(engine, locale, height):
    with browser_fixture(locale, {'width': 320, 'height': height}, engine=engine, touch=True) as (page, errors):
        page.evaluate(r"""() => {
          window.__socialStates = {};
          window.__accountResponse = request => {
            const name=request.op.replace('_account',''), action=request.args.do;
            const s=window.__socialStates[name] ||= {configured:true, connected:false, stored:false};
            if (action === 'status') return {...s};
            if (action === 'start') {
              Object.assign(s,{pending:true,user_code:'A1BC-2345',url:'https://evil.invalid/device',
                qr_png:'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='});
              return {...s};
            }
            if (action === 'poll') {
              if (!s.checked) {s.checked=true;return {pending:true,retry_in:5};}
              Object.assign(s,{pending:false,connected:true,stored:true,posts_granted:false,videos_granted:false});
              return {...s};
            }
            if (action === 'cancel') {s.pending=false;return {...s};}
            if (action === 'disconnect') {Object.assign(s,{connected:false,stored:false,pending:false});return {connected:false,revoked:false};}
            return {items:[{title:'<img id="injected" src=x onerror="window.injected=true">',description:'Synthetic account result'}]};
          };
        }""")
        page.evaluate("document.documentElement.style.fontSize='160%'")
        _open_connections(page)
        assert page.evaluate("!window.__sentUpdates.some(x=>/^(youtube|facebook|tiktok)_account$/.test(x.payload?.op||''))")
        for service, first_read, optional in [('youtube', 'channels', None), ('facebook', 'profile', 'posts'), ('tiktok', 'profile', 'videos')]:
            page.locator(f'[data-official-service="{service}"]').click()
            start = page.locator('[data-official-action="start"]')
            start.wait_for(state='visible')
            assert page.evaluate("!window.__sentUpdates.some(x=>/^(youtube|facebook|tiktok)_account$/.test(x.payload?.op||'') && x.payload.args.do==='start')") if service == 'youtube' else True
            start.click()
            page.locator('[data-official-action="poll"]').wait_for(state='visible')
            if service == 'tiktok':
                assert page.locator('.official-qr').get_attribute('src').startswith('data:image/png;base64,')
            else:
                assert page.locator('.official-code code').inner_text() == 'A1BC-2345'
                href = page.locator('.official-code a').get_attribute('href')
                assert href == ('https://www.google.com/device' if service == 'youtube' else 'https://www.facebook.com/device')
            # A provider poll is never started merely by opening this screen.
            assert page.evaluate("s=>!window.__sentUpdates.some(x=>x.payload?.op===s+'_account' && x.payload.args.do==='poll')", service)
            page.wait_for_timeout(150)
            page.locator('[data-official-action="poll"]').click()
            page.get_by_text('Sigue pendiente.' if locale == 'es' else 'Still pending.', exact=False).wait_for()
            page.locator('[data-official-action="poll"]').click()
            page.locator(f'[data-official-action="{first_read}"]').wait_for(state='visible')
            if optional:
                assert page.locator(f'[data-official-action="{optional}"]').is_disabled()
            page.locator(f'[data-official-action="{first_read}"]').click()
            page.get_by_text('Synthetic account result', exact=True).wait_for()
            assert '<img id="injected"' in page.locator('.official-result b').inner_text()
            assert page.locator('#injected').count() == 0 and not page.evaluate('!!window.injected')
            assert page.locator('#view').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
            for button in page.locator('.official-actions button').all():
                bounds = button.bounding_box()
                assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 321
                assert bounds['height'] >= 44
            # Cancelling the confirmation must not remove or revoke anything.
            before = page.evaluate("s=>window.__sentUpdates.filter(x=>x.payload?.op===s+'_account' && x.payload.args.do==='disconnect').length", service)
            page.locator('[data-official-action="disconnect"]').click()
            page.locator('#sheet').get_by_role('button', name='Cancelar' if locale == 'es' else 'Cancel', exact=True).click()
            page.locator('#sheet').wait_for(state='hidden')
            assert page.evaluate("s=>window.__sentUpdates.filter(x=>x.payload?.op===s+'_account' && x.payload.args.do==='disconnect').length", service) == before
            page.locator('[data-official-action="disconnect"]').click()
            page.locator('#sheet').get_by_role('button', name='Desconectar' if locale == 'es' else 'Disconnect', exact=True).click()
            page.get_by_text('no confirmó la revocación' if locale == 'es' else 'did not confirm revocation', exact=False).wait_for()
            assert page.locator('[data-official-action="start"]').is_visible()
            assert page.evaluate("!JSON.stringify(localStorage).includes('A1BC-2345') && !JSON.stringify(localStorage).includes('Synthetic account result')")
            page.locator('#back').click()
        assert errors == []


def test_official_connections_require_an_unchanged_verified_binding(monkeypatch):
    entry = {'chat': 7, 'addr': OWNER, 'epoch': 'fixture', 'build': 'current', 'access_session': 'one'}
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', False)
    monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', False)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', False)
    identity = Mock(return_value=(dict(entry), OWNER)); monkeypatch.setattr(live_web, '_identity', identity)
    monkeypatch.setattr(miniapp, 'compatible_build', lambda _entry: True)
    assert not miniapp._official_private(100, entry)  # unverified deployments fail closed
    identity.assert_not_called()
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', True)
    assert miniapp._official_private(100, entry)
    for field, changed in [('chat', 8), ('addr', 'other@example.org'), ('epoch', 'other'), ('build', 'old'), ('access_session', 'two')]:
        modified = dict(entry, **{field: changed})
        identity.return_value = (modified, OWNER)
        assert not miniapp._official_private(100, entry), field
    identity.return_value = (dict(entry), 'other@example.org')
    assert not miniapp._official_private(100, entry)
    identity.return_value = None  # group, unencrypted, changed key, or wrong sender
    assert not miniapp._official_private(100, entry)


def test_unverified_social_request_is_not_admitted_or_acknowledged(monkeypatch):
    monkeypatch.setattr(miniapp, '_release_allows', lambda *_args: True)
    monkeypatch.setattr(miniapp, '_official_private', lambda *_args: False)
    admit = Mock(); monkeypatch.setattr(miniapp, '_admit', admit)
    reply = Mock(); monkeypatch.setattr(miniapp, 'respond', reply)
    for op in miniapp.OFFICIAL_ACCOUNT_OPS:
        miniapp._route(17, {'chat': 7, 'addr': OWNER}, {'id': 'request', 'op': op, 'args': {'do': 'start'}})
    admit.assert_not_called(); reply.assert_not_called()


def test_private_cached_answer_and_each_fragment_recheck_identity(monkeypatch):
    entry = {'chat': 7, 'addr': OWNER, 'epoch': 'fixture', 'build': 'current', 'access_session': 'one'}
    body = json.dumps({'t': 'res', 're': 'private', 'ok': True, 'data': {'result': 'sensitive ' * 20}})
    monkeypatch.setattr(miniapp, '_answers', {'private': {'body': body, 'official_entry': entry}})
    monkeypatch.setattr(miniapp, '_status_allowed', lambda _instance: True)
    monkeypatch.setattr(miniapp, '_release_allows', lambda *_args: True)
    monkeypatch.setattr(miniapp, '_admit', lambda *_args: 'cached')
    monkeypatch.setattr(access, 'bound_allowed', lambda _entry: True)
    monkeypatch.setattr(admin, 'is_allowed', lambda _address: True)
    allowed = [False]
    monkeypatch.setattr(miniapp, '_official_private', lambda _instance, _entry: allowed[0])
    sent = []
    def send(_instance, payload, _notify=None, *, interrupted=None):
        if interrupted and interrupted():
            return False
        sent.append(payload)
        allowed[0] = False  # revoke after the first fragment leaves
        return True
    monkeypatch.setattr(chat, 'send_update', send)
    monkeypatch.setattr(miniapp, 'CHUNK', 24)
    # Even if a retry lies about its operation, the cached original remains private.
    miniapp._route(17, entry, {'id': 'private', 'retry': 1, 'op': 'hello', 'args': {}})
    assert sent == []
    allowed[0] = True
    miniapp._route(17, entry, {'id': 'private', 'retry': 2, 'op': 'hello', 'args': {}})
    assert len(sent) == 1 and sent[0]['t'] == 'chunk'


def test_private_handoff_rechecks_after_waiting_for_send_lock(monkeypatch):
    monkeypatch.setattr(miniapp, '_status_allowed', lambda _instance: True)
    allowed, sent = [True], []
    def send(_instance, payload, _notify=None, *, interrupted=None):
        allowed[0] = False  # recipient changed after the outer check
        if not interrupted():
            sent.append(payload)
            return True
        return False
    monkeypatch.setattr(chat, 'send_update', send)
    assert not miniapp._send_status(17, {'code': 'sensitive'}, private_check=lambda: allowed[0])
    assert sent == []


class Immediate:
    def submit(self, function):
        future = Future()
        try:
            future.set_result(function())
        except Exception as exc:
            future.set_exception(exc)
        return future


@pytest.mark.parametrize('engine,locale', [('chromium', 'es'), ('webkit', 'en')])
def test_browser_to_real_python_handlers_with_mocked_provider_apis(monkeypatch, tmp_path, engine, locale):
    """Drive each UI through handle(), real vault logic and fixed fake endpoints."""
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(config, 'BOT_EPOCH', 'fixture-epoch')
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', True)
    monkeypatch.setattr(config, 'MINIAPP_CURRENT_ONLY', True)
    monkeypatch.setattr(config, 'service_access_allowed', lambda _address: True)
    for name, value in {'WEB_SESSION_KEY': 'x' * 48, 'GOOGLE_OAUTH_CLIENT_ID': 'test-client',
                        'GOOGLE_OAUTH_CLIENT_SECRET': 'test-secret', 'FACEBOOK_APP_ID': '123',
                        'FACEBOOK_CLIENT_TOKEN': 'fake-client', 'FACEBOOK_READ_POSTS': '0',
                        'TIKTOK_CLIENT_KEY': 'fake-key', 'TIKTOK_CLIENT_SECRET': 'fake-secret',
                        'TIKTOK_OAUTH_REDIRECT_URI': 'https://app.example.org/callback', 'TIKTOK_READ_VIDEOS': '0'}.items():
        monkeypatch.setenv(name, value)
    for module in (yt, fb, tt):
        monkeypatch.setattr(module, '_pending', {})
    entry = {'chat': 7, 'addr': OWNER, 'epoch': 'fixture-epoch', 'build': miniapp.app_build(), 'access_session': 'one'}
    monkeypatch.setattr(state, 'data', {'apps': {'100': entry}})
    monkeypatch.setattr(state, 'maintenance_active', lambda: False)
    monkeypatch.setattr(state, 'maintenance_interrupted', lambda _generation: False)
    monkeypatch.setattr(state, 'kill_generation', lambda _address: 0)
    monkeypatch.setattr(state, 'get_kill_switch', lambda _address: False)
    monkeypatch.setattr(state, 'work_interrupted', lambda *_args: False)
    monkeypatch.setattr(state, 'guard_work', lambda *_args: nullcontext())
    monkeypatch.setattr(state, 'check_work_allowed', lambda: None)
    monkeypatch.setattr(access, 'bound_allowed', lambda _entry: True)
    monkeypatch.setattr(admin, 'is_allowed', lambda _address: True)
    monkeypatch.setattr(admin, 'seen', lambda _address: None)
    monkeypatch.setattr(miniapp, '_verified_binding', lambda _entry: True)
    monkeypatch.setattr(live_web, '_identity', lambda instance: (dict(entry), OWNER) if instance == 100 else None)
    monkeypatch.setattr(miniapp, '_workers', Immediate())
    monkeypatch.setattr(miniapp, '_answers', {})
    monkeypatch.setattr(usage, 'note', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(perf, 'note_op', lambda *_args: None)
    replies, provider_calls = [], []
    monkeypatch.setattr(miniapp, 'respond', lambda instance, rid, data, error='': replies.append((instance, rid, data, error)))

    def youtube_request(method, url, **_kwargs):
        provider_calls.append(('youtube', method, url))
        if url == yt.DEVICE_URL:
            return 200, {'device_code': 'DEVICE-SECRET', 'user_code': 'YT12-3456', 'verification_url': 'https://www.google.com/device', 'expires_in': 600}
        if url == yt.TOKEN_URL:
            return 200, {'access_token': 'YT-ACCESS-SECRET', 'refresh_token': 'YT-REFRESH-SECRET', 'expires_in': 3600, 'scope': yt.SCOPE}
        if url == yt.API_URL + 'channels':
            return 200, {'items': [{'snippet': {'title': 'Synthetic YouTube channel', 'description': 'Private fixture'}}]}
        if url == yt.REVOKE_URL:
            return 200, {}
        raise AssertionError((method, url))

    def facebook_request(method, url, **_kwargs):
        provider_calls.append(('facebook', method, url))
        if url == fb.DEVICE:
            return 200, {'code': 'FB-DEVICE-SECRET', 'user_code': 'FB12345', 'verification_uri': 'https://www.facebook.com/device', 'expires_in': 600}
        if url == fb.STATUS:
            return 200, {'access_token': 'FB-ACCESS-SECRET', 'expires_in': 3600}
        if url == fb.PERMISSIONS and method == 'GET':
            return 200, {'data': [{'permission': 'public_profile', 'status': 'granted'}]}
        if url == fb.ME:
            return 200, {'name': 'Synthetic Facebook profile'}
        if url == fb.PERMISSIONS and method == 'DELETE':
            return 200, {'success': True}
        raise AssertionError((method, url))

    def tiktok_request(method, url, **_kwargs):
        provider_calls.append(('tiktok', method, url))
        if url == tt.GET_QR:
            return 200, {'token': 'TT-DEVICE-SECRET', 'scan_qrcode_url': 'aweme://authorize?client_key=fake-key&client_ticket=initial', 'expires_in': 300}
        if url == tt.CHECK_QR:
            pending = tt._pending[OWNER]
            return 200, {'status': 'confirmed', 'client_ticket': pending['ticket'], 'state': pending['state'],
                         'redirect_uri': 'https://app.example.org/callback?code=fixture&state=' + pending['state']}
        if url == tt.TOKEN:
            return 200, {'access_token': 'TT-ACCESS-SECRET', 'refresh_token': 'TT-REFRESH-SECRET', 'expires_in': 3600,
                         'refresh_expires_in': 86400, 'scope': 'user.info.basic'}
        if url == tt.PROFILE:
            return 200, {'data': {'user': {'display_name': 'Synthetic TikTok profile'}}, 'error': {'code': 'ok'}}
        if url == tt.REVOKE:
            return 200, {}
        raise AssertionError((method, url))

    monkeypatch.setattr(yt, '_request', youtube_request)
    monkeypatch.setattr(fb, '_request', facebook_request)
    monkeypatch.setattr(tt, '_request', tiktok_request)
    def handler(request):
        before = len(replies)
        miniapp.handle(100, dict(entry), request)
        if len(replies) != before + 1:
            raise RuntimeError('synthetic request was refused')
        _instance, _rid, data, error = replies[-1]
        if error:
            raise RuntimeError(error)
        return data

    with browser_fixture(locale, {'width': 390, 'height': 844}, engine=engine, touch=True, official_handler=handler) as (page, errors):
        _open_connections(page)
        assert provider_calls == []
        for service, read_action, expected in [('youtube', 'channels', 'Synthetic YouTube channel'),
                                                ('facebook', 'profile', 'Synthetic Facebook profile'),
                                                ('tiktok', 'profile', 'Synthetic TikTok profile')]:
            page.locator(f'[data-official-service="{service}"]').click()
            page.locator('[data-official-action="start"]').wait_for(state='visible')
            calls_before_start = len(provider_calls)
            page.locator('[data-official-action="start"]').click()
            page.locator('[data-official-action="poll"]').wait_for(state='visible')
            assert len(provider_calls) == calls_before_start + 1
            if service == 'tiktok':
                assert page.locator('.official-qr').is_visible()
            page.locator('[data-official-action="poll"]').click()
            page.locator(f'[data-official-action="{read_action}"]').wait_for(state='visible')
            path = yt._path(OWNER) if service == 'youtube' else (fb.vault.path(service, OWNER))
            assert path.exists() and b'ACCESS-SECRET' not in path.read_bytes()
            if service == 'facebook':
                assert page.locator('[data-official-action="posts"]').is_disabled()
            if service == 'tiktok':
                assert page.locator('[data-official-action="videos"]').is_disabled()
            page.locator(f'[data-official-action="{read_action}"]').click()
            page.get_by_text(expected, exact=True).wait_for()
            page.locator('[data-official-action="disconnect"]').click()
            page.locator('#sheet').get_by_role('button', name='Desconectar' if locale == 'es' else 'Disconnect', exact=True).click()
            page.locator('[data-official-action="start"]').wait_for(state='visible')
            assert not path.exists()
            page.locator('#back').click()
        assert errors == []
        assert not any(secret in json.dumps(replies) for secret in ['YT-ACCESS-SECRET', 'YT-REFRESH-SECRET', 'FB-ACCESS-SECRET', 'TT-ACCESS-SECRET', 'TT-REFRESH-SECRET', 'DEVICE-SECRET'])
        assert page.evaluate("!JSON.stringify(window.__sentUpdates).includes('ACCESS-SECRET')")


class SyntheticIdentityRpc:
    """Real policy and binding checks around a disposable RPC-shaped fixture."""
    def __init__(self, address, chat_id):
        self.info = SimpleNamespace(chat_type='Single', is_encrypted=True,
                                    is_contact_request=False, is_self_talk=False)
        self.contact = SimpleNamespace(address=address, is_key_contact=True, e2ee_avail=True,
                                       is_verified=True, verifier_id=1, fingerprint='A' * 40)
        self.members = [15]
        self.message = SimpleNamespace(chat_id=chat_id, from_id=1)
        self.sent = []

    def get_message(self, _account, _instance):
        return self.message

    def get_basic_chat_info(self, _account, _chat_id):
        return self.info

    def get_chat_contacts(self, _account, _chat_id):
        return self.members

    def get_contact(self, _account, _contact_id):
        return self.contact

    def send_webxdc_status_update(self, _account, instance, payload, _description):
        self.sent.append((instance, payload))


@pytest.fixture
def live_identity(monkeypatch):
    """Exercise _official_private -> Live -> identity_guard without mocking them."""
    monkeypatch.setattr(config, 'OWNER_ADDRESS', OWNER)
    monkeypatch.setattr(config, 'OWNER_BACKUP_ADDRESS', '')
    monkeypatch.setattr(config, 'OWNER_PGP_FINGERPRINT', 'A' * 40)
    monkeypatch.setattr(config, 'OWNER_ONLY', False)
    monkeypatch.setattr(config, 'MAINTENANCE_MODE', False)
    monkeypatch.setattr(config, 'BOT_EPOCH', 'synthetic-epoch')
    monkeypatch.setattr(config, 'BOT_EPOCH_STRICT', True)
    monkeypatch.setattr(config, 'REVOKE_LEGACY_MINIAPPS', True)
    monkeypatch.setattr(config, 'MINIAPP_CURRENT_ONLY', True)
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'current-package')
    monkeypatch.setattr(access, '_controlled', False)
    monkeypatch.setattr(access, '_guests', {})
    monkeypatch.setattr(community, '_pending', set())
    monkeypatch.setattr(identity_guard, 'contact_fingerprint',
                        lambda rpc, _account, _contact: rpc.contact.fingerprint)
    rpc = SyntheticIdentityRpc(OWNER, 21)
    entry = {'chat': 21, 'addr': OWNER, 'epoch': 'synthetic-epoch',
             'build': 'current-package', 'access_session': 'owner-session'}
    monkeypatch.setattr(state, 'data', {'apps': {'100': entry}, 'invitees': {},
        'email_key_bindings': {OWNER: {'key': identity_guard._key_digest('A' * 40),
                                       'chat': 21, 'retired': []}}})
    monkeypatch.setattr(chat, '_bot', SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(chat, '_accid', 1)
    return rpc, entry


@pytest.mark.parametrize('verified,recovery,invites', list(product([False, True], repeat=3)))
def test_real_identity_policy_matrix_for_official_owner(live_identity, monkeypatch, verified, recovery, invites):
    rpc, entry = live_identity
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', verified)
    monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', recovery)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', invites)
    # Invitation mode alone verifies guests, but cannot authenticate an owner
    # without the strict owner pin or an already-bound recovery key.
    expected = verified or recovery
    assert miniapp._official_private(100, dict(entry)) is expected
    if not expected:
        return
    # Even an accepted email-recovery key does not permit plaintext, groups,
    # foreign senders, a different app message, or a replacement grant.
    for obj, attribute, bad in [(rpc.info, 'chat_type', 'Group'),
                                 (rpc.info, 'is_encrypted', False),
                                 (rpc.info, 'is_contact_request', True),
                                 (rpc.info, 'is_self_talk', True),
                                 (rpc.contact, 'address', 'other@example.invalid'),
                                 (rpc.contact, 'is_key_contact', False),
                                 (rpc.contact, 'e2ee_avail', False),
                                 (rpc.message, 'from_id', 99),
                                 (rpc.message, 'chat_id', 22)]:
        old = getattr(obj, attribute)
        setattr(obj, attribute, bad)
        assert not miniapp._official_private(100, dict(entry)), attribute
        setattr(obj, attribute, old)
    rpc.members = [15, 16]
    assert not miniapp._official_private(100, dict(entry))
    rpc.members = [15]
    assert not miniapp._official_private(100, dict(entry, access_session='another-grant'))
    assert not miniapp._official_private(100, dict(entry, build='an-old-package'))
    # Email recovery deliberately permits a currently bound encrypted key that
    # is not directly verified. Strict pinning alone does not; document this.
    rpc.contact.is_verified = False
    rpc.contact.verifier_id = None
    assert miniapp._official_private(100, dict(entry)) is recovery
    if recovery:
        state.data['email_key_bindings'][OWNER]['key'] = identity_guard._key_digest('B' * 40)
        assert not miniapp._official_private(100, dict(entry))


@pytest.mark.parametrize('verified,recovery', list(product([False, True], repeat=2)))
def test_invited_guest_always_needs_direct_verification_and_original_grant(live_identity, monkeypatch, verified, recovery):
    rpc, entry = live_identity
    guest, stamp = 'guest@example.invalid', 'a' * 32
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', verified)
    monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', recovery)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', True)
    rpc.contact.address = guest
    entry.update(addr=guest, access_session=stamp)
    state.data['invitees'][guest] = {'active': True, 'banned': False, 'chat': 21,
                                     'key': identity_guard._key_digest('A' * 40), 'stamp': stamp}
    assert miniapp._official_private(100, dict(entry))
    rpc.contact.is_verified = False
    rpc.contact.verifier_id = None
    assert not miniapp._official_private(100, dict(entry))  # email recovery never substitutes for guest SecureJoin
    rpc.contact.is_verified, rpc.contact.verifier_id = True, 1
    state.data['invitees'][guest]['stamp'] = 'b' * 32
    assert not miniapp._official_private(100, dict(entry))
    state.data['invitees'][guest]['stamp'] = stamp
    state.data['invitees'][guest]['key'] = identity_guard._key_digest('B' * 40)
    assert not miniapp._official_private(100, dict(entry))
    state.data['invitees'][guest]['key'] = identity_guard._key_digest('A' * 40)
    state.data['invitees'][guest]['active'] = False
    assert not miniapp._official_private(100, dict(entry))


def test_real_handoff_lock_rechecks_identity_after_waiting(live_identity, monkeypatch):
    rpc, entry = live_identity
    monkeypatch.setattr(config, 'BOT_VERIFIED_CONTACTS_ONLY', True)
    monkeypatch.setattr(config, 'EMAIL_KEY_RECOVERY', False)
    monkeypatch.setattr(config, 'INVITE_AUTO_ACCESS', False)
    entered = threading.Event()
    actual_send = chat.send_update

    def entered_send(*args, **kwargs):
        entered.set()
        return actual_send(*args, **kwargs)

    monkeypatch.setattr(chat, 'send_update', entered_send)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with chat._lock:
            future = pool.submit(miniapp._send_status, 100, {'test': 'sensitive fixture'},
                                 private_check=lambda: miniapp._official_private(100, dict(entry)))
            assert entered.wait(3)  # initial authorization passed, real chat lock is held
            rpc.info.is_encrypted = False
        assert future.result(timeout=5) is False
    assert rpc.sent == []
