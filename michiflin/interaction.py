"""Explicit private-chat interface choice, separate from admission and AI consent.

Only authenticated private intake calls this module. A preference grants no
identity, admin, API or provider permission, and never replays the first message.
"""
from __future__ import annotations

import copy
from typing import Any

from . import access, chat, config, state
from .util import tr

def _identity(chat_id: int) -> tuple[str, str]:
    address = str(chat.sender() or '').strip().casefold()
    if not address or int(chat_id) <= 0:
        raise ValueError('a private bound sender is required')
    return address, access.session_stamp(address)


def record(chat_id: int) -> dict[str, Any]:
    address, stamp = _identity(chat_id)
    with state.lock:
        item = state.data.get('chat_interfaces', {}).get(f'{address}:{int(chat_id)}')
        if (isinstance(item, dict) and item.get('chat') == int(chat_id)
                and item.get('epoch') == config.BOT_EPOCH and item.get('grant') == stamp):
            return dict(item)
    return {}


def _write(chat_id: int, mode: str = '', *, prompted: bool = True) -> None:
    address, stamp = _identity(chat_id)
    with state.lock:
        records = state.data.setdefault('chat_interfaces', {})
        key = f'{address}:{int(chat_id)}'
        previous = copy.deepcopy(records.get(key))
        records[key] = {'chat': int(chat_id), 'epoch': config.BOT_EPOCH, 'grant': stamp,
                            'mode': mode, 'prompted': prompted}
        try:
            state.save()
        except Exception:
            if previous is None:
                records.pop(key, None)
            else:
                records[key] = previous
            raise


def choose(chat_id: int, mode: str, *, sync_setting: bool = True) -> None:
    if mode not in {'app', 'full'}:
        raise ValueError('invalid interface choice')
    # This is a per-sender, per-chat and per-grant decision. Shared defaults
    # (including the owner's preferred mode) never count as a guest opting in.
    _write(chat_id, mode)
    if sync_setting and not state.set_setting('chat_mode', mode):
        raise ValueError('could not save chat mode')


def forget(chat_id: int) -> None:
    address, _ = _identity(chat_id)
    with state.lock:
        state.data.setdefault('chat_interfaces', {}).pop(f'{address}:{int(chat_id)}', None)
        state.save()


def menu(locale: str, *, blocked: bool = False) -> str:
    text = tr(locale,
        '¿Cómo prefieres usar Michiflin?\n\n'
        'Miniapp: abre el archivo que te envié o escribe /app.\n'
        'Solo chat: escribe /comandos para empezar sin miniapp.\n\n'
        'Puedes cambiarlo después con /modo.',
        'How would you like to use Michiflin?\n\n'
        'Miniapp: open the file I sent or type /app.\n'
        'Chat only: type /commands to get started without the miniapp.\n\n'
        'You can change this later with /mode.')
    if blocked:
        text += tr(locale, ' No ejecuté esta solicitud.', ' I did not execute this request.')
    return text


def guard(chat_id: int, locale: str, *, safe: bool = False) -> bool:
    """True consumes the input without running it, forwarding it or deleting it."""
    if safe:
        return False  # mode selection, authenticated emergency/status controls
    current = record(chat_id)
    mode = current.get('mode')
    if mode == 'full':
        return False
    if mode == 'app':
        chat.text(chat_id, tr(locale,
            'Abre la miniapp de este chat. Para usar solo comandos, escribe /comandos.',
            'Open the miniapp in this chat. To use commands instead, type /commands.'))
        return True
    if not current.get('prompted'):
        if chat.text(chat_id, menu(locale, blocked=True)):
            _write(chat_id)
    else:
        chat.text(chat_id, tr(locale, 'Elige /app o /comandos. No ejecuté el mensaje.',
                                    'Choose /app or /commands. I did not execute the message.'))
    return True
