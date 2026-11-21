"""Deliver one exact, owner-requested update using the existing verified binding.

No new account, contact, invitation, grant or access-policy change is made.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def deliver() -> dict:
    from deltachat2 import IOTransport, Rpc
    from michiflin import chat, config, miniapp, state
    revision = os.getenv('OWNER_UI_REFRESH_REVISION', '')
    running = os.getenv('RUNNING_SHA', '')
    if (not re.fullmatch('[0-9a-f]{40}', revision) or revision != running
            or not config.OWNER_ONLY or config.INVITE_AUTO_ACCESS or not config.OWNER_ADDRESS):
        raise ValueError('owner refresh is not armed for this exact revision')
    state.load()
    entries = [entry for entry in miniapp._apps().values() if isinstance(entry, dict)
               and str(entry.get('addr') or '').casefold() == config.OWNER_ADDRESS.casefold()
               and entry.get('epoch') == config.BOT_EPOCH]
    if any(entry.get('recovery_revision') == revision for entry in entries):
        return {'registered': True, 'delivery': 'previously_registered', 'duplicate_sent': False,
                'revision': revision, 'build': miniapp.app_build()}
    entries.sort(key=lambda entry: float(entry.get('sent') or 0), reverse=True)
    with IOTransport(accounts_dir=str(config.CONFIG_DIR / 'accounts')) as transport:
        rpc = Rpc(transport)
        accounts = rpc.get_all_account_ids()
        if len(accounts) != 1 or not rpc.is_configured(accounts[0]):
            raise ValueError('one configured existing account is required')
        account = accounts[0]
        chat.bind(SimpleNamespace(rpc=rpc), account)
        selected = next((entry for entry in entries if miniapp._verified_binding(entry)), None)
        if not selected:
            raise ValueError('no current verified owner app binding')
        rpc.start_io(account)
        try:
            mid = miniapp.send_app(int(selected['chat']), 'es', updated=True,
                                   address=config.OWNER_ADDRESS, recovery_revision=revision)
            if not mid:
                raise ValueError('owner refresh was not registered')
            outcome = chat.wait_delivery(mid, 180, threading.Event())
            state.save()
            return {'registered': True, 'delivery': outcome, 'duplicate_sent': False,
                    'revision': revision, 'build': miniapp.app_build(), 'previous_instances_preserved': True}
        finally:
            rpc.stop_io(account)


if __name__ == '__main__':
    try:
        result = deliver()
        Path('owner-refresh-receipt.json').write_text(json.dumps(result, indent=2))
        print('SAFE_OWNER_REFRESH ' + json.dumps(result))
    except Exception as exc:
        print('SAFE_OWNER_REFRESH_FAILED ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
