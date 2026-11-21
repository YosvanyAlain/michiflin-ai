"""Opt-in group-only public tools; no AI, personal APIs, files, queue or private history."""
from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from deltachat2 import MessageData
from . import chat, config, identity_guard, state, tools

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='public-group')
_slots = threading.BoundedSemaphore(4)
_last: dict[int, float] = {}
_lock = threading.RLock()
_active_count = 0


def busy() -> int:
    with _lock:
        return _active_count


def owner_verified(rpc, account: int, contact_id: int) -> bool:
    contact = rpc.get_contact(account, contact_id)
    if (str(contact.address or '').casefold() != config.OWNER_ADDRESS.casefold()
            or contact.is_key_contact is not True or contact.e2ee_avail is not True):
        return False
    fingerprint = identity_guard.contact_fingerprint(rpc, account, contact_id)
    if not fingerprint:
        return False
    if config.EMAIL_KEY_RECOVERY:
        binding = state.data.get('email_key_bindings', {}).get(config.OWNER_ADDRESS.casefold(), {})
        return bool(binding and binding.get('key') == identity_guard._key_digest(fingerprint))
    return fingerprint == identity_guard.normalize_fingerprint(config.OWNER_PGP_FINGERPRINT)


def owner_present(rpc, account: int, chat_id: int) -> bool:
    record = state.data.get('public_groups', {}).get(str(chat_id), {})
    approved_key = record.get('owner_key', '') if isinstance(record, dict) else ''
    for cid in rpc.get_chat_contacts(account, chat_id):
        if owner_verified(rpc, account, int(cid)):
            return True
        if approved_key:
            contact = rpc.get_contact(account, int(cid))
            if (str(contact.address or '').casefold() == config.OWNER_ADDRESS.casefold()
                    and identity_guard.contact_fingerprint(rpc, account, int(cid)) == approved_key):
                return True
    return False


def parse(text: str) -> tuple[str, str] | None:
    value = text.strip()
    match = re.fullmatch(r'/(michiflin|tasas|rates|clima|weather|buscar|search|ayuda|help)(?:\s+([\s\S]{0,300}))?', value, re.I)
    if not match:
        return None
    command, argument = match.group(1).casefold(), (match.group(2) or '').strip()
    if command == 'michiflin':
        control = argument.casefold()
        if control in {'activar', 'enable', 'desactivar', 'disable'}:
            return ('enable' if control in {'activar', 'enable'} else 'disable'), ''
        return ('search', argument) if argument else ('help', '')
    return {'tasas': 'rates', 'clima': 'weather', 'buscar': 'search', 'ayuda': 'help'}.get(command, command), argument


def public_answer(command: str, argument: str, locale: str = 'es') -> str:
    if command == 'rates':
        data = tools.rates_snapshot()
        values = data.get('rates', {})
        rows = [f'{code}: {values[code]} CUP' for code in ['USD', 'EUR', 'MLC', 'CAD', 'MXN', 'TRX'] if code in values]
        if data.get('stale'):
            rows.append('Última lectura guardada; la actualización actual no está disponible.')
        try:
            if float(data.get('at') or 0) > 0:
                rows.append('Lectura recibida: ' + datetime.fromtimestamp(float(data['at']), config.LOCAL_ZONE).isoformat(timespec='minutes'))
        except (TypeError, ValueError, OverflowError):
            pass
        return 'Michiflin · elTOQUE\n' + ('\n'.join(rows) if rows else 'No hay una lectura disponible.')
    if command == 'weather':
        try:
            data = tools.weather_data(argument or 'La Habana, Cuba', locale)
            current = data['current']
            rows = [str(data['place']['label']), f"{current['description']} · {current['temp']} °C",
                    f"Humedad: {current['humidity']}% · Viento: {current['wind']} km/h"]
            for day in data.get('days', [])[:4]:
                rows.append(f"{day['date']}: {day['description']} · {day['low']}–{day['high']} °C")
            rows.append('Open-Meteo · ' + str(data.get('as_of', '')))
            return 'Michiflin · Clima\n' + '\n'.join(rows)
        except tools.WeatherError:
            return 'Michiflin · No pude consultar ese clima ahora. Prueba /clima ciudad, país.'
    if command == 'search' and argument:
        results = tools.search(argument, locale, limit=4, engine='standard')
        return 'Michiflin · Búsqueda pública sin IA ni cuota de modelos\n\n' + ('\n\n'.join(
            str(item.title)[:180] + '\n' + str(item.snippet)[:350] + '\n' + str(item.url) for item in results)[:3400]
            or 'La fuente no respondió o no hubo resultados. Inténtalo más tarde.')
    return ('Michiflin en grupos: /buscar consulta · /tasas · /clima ciudad.\n'
            'No se usan modelos de IA ni sus cuotas. Las APIs personales, agente con IA, programador, archivos y descargas solo están disponibles en el chat individual. '
            'El dueño puede activar o desactivar este grupo con /michiflin activar o /michiflin desactivar.')


def _enabled(rpc, account: int, chat_id: int) -> bool:
    if config.MAINTENANCE_MODE or state.maintenance_active() or state.get_kill_switch(config.OWNER_ADDRESS):
        return False
    if not state.data.get('public_groups', {}).get(str(chat_id), {}).get('enabled'):
        return False
    info = rpc.get_basic_chat_info(account, chat_id)
    kind = str(getattr(info.chat_type, 'value', info.chat_type))
    return (kind == 'Group' and getattr(info, 'is_encrypted', False) is True
            and not getattr(info, 'is_contact_request', False) and owner_present(rpc, account, chat_id))


def _send(rpc, account: int, chat_id: int, answer: str, *, control: bool = False) -> None:
    if control or _enabled(rpc, account, chat_id):
        mid = int(rpc.send_msg(account, chat_id, MessageData(text=answer[:4000])))
        if mid:
            chat._recent_out.append((mid, time.time()))


def offer(bot, account: int, message, info) -> bool:
    global _active_count
    if (os.getenv('GROUP_PUBLIC_TOOLS', '0') != '1' or getattr(info, 'is_encrypted', False) is not True
            or str(getattr(info.chat_type, 'value', info.chat_type)) != 'Group'
            or getattr(message, 'show_padlock', False) is not True or getattr(message, 'file', None)):
        return False
    parsed = parse(str(message.text or ''))
    if not parsed:
        return False
    command, argument = parsed
    rpc, chat_id = bot.rpc, int(message.chat_id)
    try:
        if command in {'enable', 'disable'}:
            if not owner_verified(rpc, account, int(message.from_id)):
                # Legacy identities may have no global owner pin. Do not
                # silently weaken verification: request explicit private-app approval.
                contact = rpc.get_contact(account, int(message.from_id))
                if command != 'enable' or str(contact.address or '').casefold() != config.OWNER_ADDRESS.casefold():
                    return False
                if not identity_guard.contact_fingerprint(rpc, account, int(message.from_id)):
                    return False
                with state.lock:
                    groups = state.data.setdefault('public_groups', {})
                    previous = groups.get(str(chat_id), {})
                    if time.time() - float(previous.get('requested', 0)) < 60:
                        return True
                    if len(groups) >= 20 and str(chat_id) not in groups:
                        return False
                    groups[str(chat_id)] = {**previous, 'enabled': False, 'requested': time.time(),
                                            'title': str(getattr(info, 'name', '') or '')[:120]}
                    state.save()
                _send(rpc, account, chat_id, 'Michiflin · Confirma la activación desde mi miniapp en el chat individual: Ajustes → Grupos públicos. Solo búsqueda sin IA, tasas y clima.', control=True)
                return True
            if command == 'enable' and getattr(info, 'is_contact_request', False):
                rpc.accept_chat(account, chat_id)  # only the already-pinned owner may admit this group
            with state.lock:
                state.data.setdefault('public_groups', {})[str(chat_id)] = {'enabled': command == 'enable', 'updated': time.time()}
                state.save()
            _send(rpc, account, chat_id, 'Michiflin · herramientas públicas del grupo ' + ('activadas.' if command == 'enable' else 'desactivadas.'), control=True)
            return True
        if not _enabled(rpc, account, chat_id):
            if owner_verified(rpc, account, int(message.from_id)):
                with _lock:
                    if time.monotonic() - _last.get(chat_id, 0) < 10:
                        return True
                    _last[chat_id] = time.monotonic()
                _send(rpc, account, chat_id, 'Michiflin · Este grupo aún no está activado. Escribe /michiflin activar. Herramientas públicas: /buscar, /tasas y /clima.', control=True)
                return True
            return False
        with _lock:
            if time.monotonic() - _last.get(chat_id, 0) < 3 or not _slots.acquire(blocking=False):
                return False  # short anti-flood backpressure, not a daily AI quota
            _active_count += 1
            _last[chat_id] = time.monotonic()
            if len(_last) > 256:
                _last.pop(next(iter(_last)))
        def work():
            global _active_count
            try:
                answer = public_answer(command, argument)
                _send(rpc, account, chat_id, answer)
            except Exception:
                # No provider response, address, key or private exception enters a group.
                pass
            finally:
                with _lock:
                    _active_count -= 1
                _slots.release()
        try:
            _pool.submit(work)
        except Exception:
            with _lock:
                _active_count -= 1
            _slots.release()
            raise
        return True
    except Exception:
        return False


def manage(private_chat: int, args: dict) -> dict:
    """Approval is accepted only through the owner's encrypted individual app."""
    if chat.sender().casefold() != config.OWNER_ADDRESS.casefold():
        raise ValueError('owner_required')
    rpc, account = chat.rpc(), chat.account()
    info = rpc.get_basic_chat_info(account, private_chat)
    if str(getattr(info.chat_type, 'value', info.chat_type)) != 'Single' or getattr(info, 'is_encrypted', False) is not True:
        raise ValueError('private_encrypted_chat_required')
    action = args.get('do', 'status')
    if action in {'enable', 'disable'}:
        group_id = int(args.get('id', 0))
        record = state.data.get('public_groups', {}).get(str(group_id))
        if not isinstance(record, dict):
            raise ValueError('unknown_group')
        if action == 'enable':
            if os.getenv('GROUP_PUBLIC_TOOLS', '0') != '1':
                raise ValueError('groups_disabled')
            if not record.get('enabled') and time.time() - float(record.get('requested', 0)) > 86400:
                raise ValueError('request_expired')
            group = rpc.get_basic_chat_info(account, group_id)
            if str(getattr(group.chat_type, 'value', group.chat_type)) != 'Group' or group.is_encrypted is not True:
                raise ValueError('encrypted_group_required')
            peers = set(rpc.get_chat_contacts(account, private_chat)) & set(rpc.get_chat_contacts(account, group_id))
            owner = next((int(cid) for cid in peers if str(rpc.get_contact(account, cid).address or '').casefold() == config.OWNER_ADDRESS.casefold()), 0)
            fingerprint = identity_guard.contact_fingerprint(rpc, account, owner) if owner else ''
            if not fingerprint:
                raise ValueError('owner_key_required')
            if getattr(group, 'is_contact_request', False):
                rpc.accept_chat(account, group_id)
            record = {**record, 'enabled': True, 'owner_key': fingerprint, 'updated': time.time()}
        else:
            record = {**record, 'enabled': False, 'updated': time.time()}
        with state.lock:
            state.data.setdefault('public_groups', {})[str(group_id)] = record
            state.save()
        _send(rpc, account, group_id, 'Michiflin · herramientas públicas del grupo ' + ('activadas. /ayuda' if action == 'enable' else 'desactivadas.'), control=True)
    elif action != 'status':
        raise ValueError('action')
    return {'available': os.getenv('GROUP_PUBLIC_TOOLS', '0') == '1', 'groups': [
        {'id': int(gid), 'title': str(row.get('title') or 'Grupo ' + gid)[:120], 'enabled': bool(row.get('enabled')),
         'pending': not row.get('enabled') and time.time() - float(row.get('requested', 0)) < 86400}
        for gid, row in list(state.data.get('public_groups', {}).items())[-20:] if isinstance(row, dict)]}
