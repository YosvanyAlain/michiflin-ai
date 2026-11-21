"""Owner-authorized disposable visitor. No production account RPC or mail I/O.

The restored snapshot is opened via read-only SQLite solely to find the exact
already-authorized public invitation using its audited hashes. Nothing is
printed except booleans/counts. No snapshot is saved or uploaded. This tool is
on a diagnostics-only branch, not a runtime or scheduled workflow.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import urlencode

from deltachat2 import IOTransport, Rpc
from deltachat2.types import MessageData

EXPECTED = {'address': '4a192f25d0f1b4c64124ce00b43b8d63b5f141b00deaa72226f3eac726c11ebc', 'invite': '3614434a2355980e907e35e184e44eeeb84505403555a42abc587bfd25dc337f', 'auth': '9e771c0cd4e0c0ffddec8724b158c99579d8de1c0a3d03d80b0df1efffb29bda'}
FINGERPRINT = '45824F71C9950BC6C2B1E8ABA2B91F8FF7624438'
RESULT = Path(os.environ['RUNNER_TEMP']) / 'silent-visitor-result.json'
report = {'production_account_rpc_started': False, 'snapshot_modified': False}


def invitation():
    root = Path(os.environ['CONFIG_DIR'])
    files = list((root / 'accounts').rglob('dc.db'))
    assert len(files) == 1
    with sqlite3.connect('file:' + str(files[0]) + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        address = next(v for k, v in db.execute('SELECT keyname,value FROM config')
                       if k in {'addr', 'configured_addr'} and hashlib.sha256(v.strip().lower().encode()).hexdigest() == EXPECTED['address'])
        tokens = list(db.execute('SELECT namespc,token FROM tokens'))
        invite = next(v for ns, v in tokens if ns == 100 and hashlib.sha256(v.encode()).hexdigest() == EXPECTED['invite'])
        auth = next(v for ns, v in tokens if ns == 110 and hashlib.sha256(v.encode()).hexdigest() == EXPECTED['auth'])
    link = 'https://i.delta.chat/#' + FINGERPRINT + '&' + urlencode({'v': '3', 'i': invite, 's': auth, 'a': address})
    # Destroy the isolated restored account before starting even the visitor RPC.
    shutil.rmtree(root)
    return link


def wait_for(fn, timeout=150):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(1)
    raise TimeoutError('visitor phase timed out')


def base36(n):
    chars = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = ''
    while n:
        n, i = divmod(n, 36)
        out = chars[i] + out
    return out


def run():
    link = invitation()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='silent-visitor-') as directory:
        with IOTransport(directory, stderr=subprocess.DEVNULL) as transport:
            rpc = Rpc(transport)
            account = rpc.add_account()
            rpc.set_config_from_qr(account, 'dcaccount:https://nine.testrun.org/new')
            rpc.set_config(account, 'displayname', 'Private onboarding check')
            if not rpc.is_configured(account):
                rpc.configure(account)
            rpc.start_io(account)
            cid = rpc.secure_join(account, link)
            def messages():
                return [rpc.get_message(account, m) for m in rpc.get_message_ids(account, cid, False, False) if m > 9]
            def incoming():
                return [m for m in messages() if m.from_id != 1 and not m.is_info]
            def attachment():
                return next((m for m in incoming() if str(m.file or '').endswith('.xdc')), None)
            app = wait_for(attachment)
            report['miniapp_received'] = True
            report['caption_empty'] = not (app.text or '').strip()
            report['securejoin_verified'] = bool(rpc.get_contact(account, app.from_id).is_verified)
            report['encrypted_attachment'] = bool(app.show_padlock)
            report['only_one_initial_attachment_no_text'] = len(incoming()) == 1
            # Exercise the real miniapp server protocol, not mocked handlers.
            rid = base36(int(time.time() * 1000)) + ''.join(secrets.choice('0123456789abcdefghijklmnopqrstuvwxyz') for _ in range(5))
            rpc.send_webxdc_status_update(account, app.id, json.dumps({'payload': {'t': 'req', 'id': rid, 'op': 'hello', 'args': {}, 'lang': 'en'}}), '')
            def hello():
                payloads = [u.get('payload', {}) for u in json.loads(rpc.get_webxdc_status_updates(account, app.id, 0))]
                normal = next((p for p in payloads if p.get('t') == 'res' and p.get('re') == rid), None)
                if normal:
                    return normal
                chunks = [p for p in payloads if p.get('t') == 'chunk' and p.get('re') == rid]
                if chunks and len({p['i'] for p in chunks}) == chunks[0]['n']:
                    return json.loads(''.join(p['s'] for p in sorted(chunks, key=lambda p: p['i'])))
            response = wait_for(hello)
            assert response.get('ok'), 'hello did not succeed'
            report['hello_success'] = True
            report['ordinary_non_admin'] = response['data'].get('admin') is False
            def exchange(text):
                before = {m.id for m in incoming()}
                rpc.send_msg(account, cid, MessageData(text=text))
                return wait_for(lambda: next((m for m in incoming() if m.id not in before and not m.file), None))
            reply = exchange('hello, how can I use this?')
            report['english_choice_without_ai'] = 'How would you like' in reply.text and '/commands' in reply.text and '/app' in reply.text
            reply = exchange('/commands')
            report['explicit_chat_help'] = 'Available command features' in reply.text and 'require the miniapp' in reply.text
            reply = exchange('/calc 2+2')
            report['command_after_choice'] = '4' in reply.text and 'How would you like' not in reply.text
            reply = exchange('/mode')
            report['later_switch_available'] = '/commands' in reply.text and '/app' in reply.text
            before = {m.id for m in incoming()}
            rpc.send_msg(account, cid, MessageData(text='/app'))
            wait_for(lambda: next((m for m in incoming() if m.id not in before and str(m.file or '').endswith('.xdc')), None))
            report['explicit_return_to_miniapp'] = True
            reply = exchange('/calc 5+5')
            report['app_mode_no_command_execution'] = 'To use commands instead' in reply.text and '10' not in reply.text
            report['elapsed_seconds'] = round(time.monotonic() - started)
            rpc.stop_io(account)
    required = [v for k, v in report.items() if isinstance(v, bool) and k not in {'production_account_rpc_started', 'snapshot_modified'}]
    assert required and all(required), 'one or more visitor checks failed'


try:
    run()
except Exception as exc:
    report['failure_class'] = type(exc).__name__
    RESULT.write_text(json.dumps(report, indent=2))
    print('DISPOSABLE_VISITOR_FAILED', type(exc).__name__)
    raise SystemExit(1)
else:
    RESULT.write_text(json.dumps(report, indent=2))
    print('DISPOSABLE_VISITOR_ALL_CHECKS_PASSED')
