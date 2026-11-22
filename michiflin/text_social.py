"""Text-only community for admitted bot contacts; no email in public responses.

The server can read posts for delivery/moderation. Transport is the existing
verified private chat; this is NOT end-to-end encryption between members.
The SQLite database lives inside CONFIG_DIR and its consistent snapshots are
included in the existing encrypted state backup.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import re
import secrets
import sqlite3
import time
from typing import Any

from . import access, admin, config

MAX_POSTS = 20000
THEMES = {'auto', 'violet', 'ocean', 'forest', 'rose', 'amber'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS profiles (
 id TEXT PRIMARY KEY, scope TEXT UNIQUE NOT NULL, alias TEXT NOT NULL,
 alias_key TEXT UNIQUE NOT NULL, bio TEXT NOT NULL DEFAULT '',
 prefs TEXT NOT NULL DEFAULT '{}', banned INTEGER NOT NULL DEFAULT 0,
 admin INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS posts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT NOT NULL, body TEXT NOT NULL,
 parent INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, edited REAL NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
 hidden INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
 nonce TEXT NOT NULL, UNIQUE(author,nonce));
CREATE INDEX IF NOT EXISTS social_feed ON posts(parent,deleted,hidden,id DESC);
CREATE TABLE IF NOT EXISTS relations (
 actor TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
 PRIMARY KEY(actor,kind,target));
CREATE TABLE IF NOT EXISTS reports (
 actor TEXT NOT NULL, post INTEGER NOT NULL, reason TEXT NOT NULL,
 created REAL NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(actor,post));
'''


class SocialError(ValueError):
    pass


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise SocialError('invalid_id')
    return value


def _text(value: Any, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise SocialError('invalid_text')
    value = value.strip()
    if required and not value:
        raise SocialError('empty_text')
    return value


def _scope(address: str) -> str:
    address = str(address or '').strip().casefold()
    if not address or not admin.is_allowed(address) or not config.service_access_allowed(address):
        raise SocialError('denied')
    stamp = 'owner' if config.is_owner_admin(address) else access.session_stamp(address)
    if not stamp:
        if config.INVITE_AUTO_ACCESS or access.snapshot().get('controlled'):
            raise SocialError('denied')
        stamp = 'legacy'
    epoch = config.BOT_EPOCH if config.BOT_EPOCH_STRICT else ''
    return hashlib.sha256((address + '\0' + stamp + '\0' + epoch).encode()).hexdigest()


@contextmanager
def database():
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = config.CONFIG_DIR / 'text-social.sqlite3'
    with closing(sqlite3.connect(path, timeout=15)) as db:
        path.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _profile(db, scope, is_admin):
    row = db.execute('SELECT * FROM profiles WHERE scope=?', (scope,)).fetchone()
    if row is None:
        pid = secrets.token_hex(12)
        alias = 'Persona-' + pid[:8]
        db.execute('INSERT INTO profiles(id,scope,alias,alias_key,admin) VALUES(?,?,?,?,?)',
                   (pid, scope, alias, alias.casefold(), int(is_admin)))
        row = db.execute('SELECT * FROM profiles WHERE id=?', (pid,)).fetchone()
    if row['banned'] and not is_admin:
        raise SocialError('suspended')
    if bool(row['admin']) != is_admin:
        db.execute('UPDATE profiles SET admin=? WHERE id=?', (int(is_admin), row['id']))
        row = db.execute('SELECT * FROM profiles WHERE id=?', (row['id'],)).fetchone()
    return row


def _public(row):
    return {'id': row['id'], 'alias': row['alias'], 'bio': row['bio'], 'moderator': bool(row['admin'])}


def _relation(db, actor, kind, target):
    return db.execute('SELECT 1 FROM relations WHERE actor=? AND kind=? AND target=?', (actor, kind, str(target))).fetchone() is not None


def _post(db, post_id):
    row = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if row is None:
        raise SocialError('not_found')
    return row


def _can_read(db, row, actor, moderator=False):
    if row['deleted'] or (row['hidden'] and not moderator):
        return False
    if not moderator and (db.execute('SELECT banned FROM profiles WHERE id=?', (row['author'],)).fetchone()['banned']):
        return False
    if row['parent']:
        parent = _post(db, row['parent'])
        if not _can_read(db, parent, actor, moderator):
            return False
    return not _relation(db, actor, 'mute', row['author']) and not _relation(db, actor, 'hide', row['id'])


def _serialize(db, row, actor, moderator):
    profile = db.execute('SELECT * FROM profiles WHERE id=?', (row['author'],)).fetchone()
    return {'id': row['id'], 'author': _public(profile), 'text': row['body'], 'parent': row['parent'],
            'created': row['created'], 'edited': row['edited'], 'revision': row['revision'],
            'pinned': bool(row['pinned']), 'hidden': bool(row['hidden']),
            'mine': row['author'] == actor, 'can_moderate': moderator,
            'likes': db.execute("SELECT count(*) FROM relations WHERE kind='like' AND target=?", (str(row['id']),)).fetchone()[0],
            'liked': _relation(db, actor, 'like', row['id']), 'saved': _relation(db, actor, 'save', row['id']),
            'following': _relation(db, actor, 'follow', row['author']),
            'replies': db.execute('SELECT count(*) FROM posts WHERE parent=? AND deleted=0 AND hidden=0', (row['id'],)).fetchone()[0]}


def operate(address: str, args: dict[str, Any]) -> dict[str, Any]:
    scope = _scope(address)  # recheck current admission, not a client-supplied actor
    moderator = config.is_owner_admin(address)
    action = args.get('do', 'feed')
    if not isinstance(action, str):
        raise SocialError('unknown_action')
    with database() as db:
        person = _profile(db, scope, moderator)
        actor = person['id']
        if action == 'profile':
            alias = _text(args.get('alias', person['alias']), 36, required=True)
            if not re.fullmatch(r'[\w .\-]{2,36}', alias, re.UNICODE) or ('admin' in alias.casefold() or 'michiflin' in alias.casefold()) and not moderator:
                raise SocialError('invalid_alias')
            bio = _text(args.get('bio', person['bio']), 240)
            prefs = args.get('prefs', json.loads(person['prefs']))
            if (not isinstance(prefs, dict) or set(prefs) - {'theme', 'density', 'font', 'muted_tags'}
                    or any(not isinstance(prefs.get(k, ''), str) for k in ('theme', 'density', 'font'))
                    or prefs.get('theme', 'auto') not in THEMES or prefs.get('density', 'comfortable') not in {'comfortable', 'compact'}
                    or prefs.get('font', 'normal') not in {'normal', 'large'}):
                raise SocialError('invalid_preferences')
            tags = prefs.get('muted_tags', [])
            if not isinstance(tags, list) or len(tags) > 30 or any(not isinstance(t, str) or not re.fullmatch(r'[\w]{1,32}', t) for t in tags):
                raise SocialError('invalid_preferences')
            try:
                db.execute('UPDATE profiles SET alias=?,alias_key=?,bio=?,prefs=? WHERE id=?', (alias, alias.casefold(), bio, json.dumps(prefs), actor))
            except sqlite3.IntegrityError:
                raise SocialError('alias_taken') from None
        elif action == 'post':
            text = _text(args.get('text'), 3000, required=True)
            nonce = _text(args.get('nonce'), 80, required=True)
            if not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', nonce):
                raise SocialError('invalid_nonce')
            parent = _integer(args.get('parent', 0))
            existing = db.execute('SELECT id,body,parent FROM posts WHERE author=? AND nonce=?', (actor, nonce)).fetchone()
            if existing:
                if existing['body'] != text or existing['parent'] != parent:
                    raise SocialError('conflict')
                return {'created_id': existing['id'], 'duplicate': True}
            if parent:
                root = _post(db, parent)
                if root['parent'] or not _can_read(db, root, actor, moderator):
                    raise SocialError('not_found')
            recent = db.execute('SELECT count(*),max(created) FROM posts WHERE author=? AND created>?', (actor, time.time()-600)).fetchone()
            if recent[0] >= 20 or recent[1] and time.time() - recent[1] < 8:
                raise SocialError('rate_limit')
            if db.execute('SELECT count(*) FROM posts').fetchone()[0] >= MAX_POSTS:
                raise SocialError('capacity')  # never silently purge history
            now = time.time()
            cursor = db.execute('INSERT INTO posts(author,body,parent,created,edited,nonce) VALUES(?,?,?,?,?,?)', (actor, text, parent, now, now, nonce))
            return {'created_id': cursor.lastrowid}
        elif action in {'edit', 'delete', 'like', 'save', 'hide', 'report', 'pin', 'moderate'}:
            row = _post(db, _integer(args.get('id'), 1))
            if action not in {'delete', 'moderate'} and not _can_read(db, row, actor, moderator):
                raise SocialError('not_found')
            if action in {'edit', 'delete'}:
                if row['author'] != actor:
                    raise SocialError('denied')
                if row['revision'] != _integer(args.get('revision'), 1):
                    raise SocialError('conflict')
                body = _text(args.get('text'), 3000, required=True) if action == 'edit' else ''
                db.execute('UPDATE posts SET body=?,deleted=?,edited=?,revision=revision+1 WHERE id=?', (body, int(action == 'delete'), time.time(), row['id']))
            elif action == 'report':
                reason = _text(args.get('reason'), 500, required=True)
                db.execute('INSERT OR IGNORE INTO reports(actor,post,reason,created) VALUES(?,?,?,?)', (actor, row['id'], reason, time.time()))
            elif action in {'pin', 'moderate'}:
                if not moderator or type(args.get('on')) is not bool:
                    raise SocialError('denied')
                column = 'pinned' if action == 'pin' else 'hidden'
                db.execute(f'UPDATE posts SET {column}=? WHERE id=?', (int(args['on']), row['id']))
                if action == 'moderate':
                    db.execute('UPDATE reports SET resolved=1 WHERE post=?', (row['id'],))
            else:
                _set_relation(db, actor, action, str(row['id']), args.get('on'))
        elif action in {'follow', 'mute', 'ban'}:
            target = _text(args.get('person'), 24, required=True)
            target_row = db.execute('SELECT * FROM profiles WHERE id=?', (target,)).fetchone()
            if target_row is None or target == actor:
                raise SocialError('invalid_person')
            if action == 'ban':
                if not moderator or target_row['admin'] or type(args.get('on')) is not bool:
                    raise SocialError('denied')
                db.execute('UPDATE profiles SET banned=? WHERE id=?', (int(args['on']), target))
            else:
                _set_relation(db, actor, action, target, args.get('on'))
        elif action == 'restore_hidden':
            db.execute("DELETE FROM relations WHERE actor=? AND kind IN ('hide','mute')", (actor,))
        elif action not in {'feed', 'thread', 'me', 'moderation'}:
            raise SocialError('unknown_action')
        person = db.execute('SELECT * FROM profiles WHERE id=?', (actor,)).fetchone()
        result = {'me': _public(person), 'prefs': json.loads(person['prefs']), 'posts': [], 'next': 0,
                  'privacy': 'community-server-readable-encrypted-backups', 'limit': 3000}
        if action == 'moderation':
            if not moderator:
                raise SocialError('denied')
            result['reports'] = [dict(r) for r in db.execute('SELECT post,reason,created,resolved FROM reports ORDER BY created DESC LIMIT 100')]
            result['people'] = [{**_public(r), 'banned': bool(r['banned'])} for r in db.execute('SELECT * FROM profiles LIMIT 500')]
            return result
        parent = _integer(args.get('parent', 0)) if action == 'thread' else 0
        if action == 'thread' and not parent:
            raise SocialError('invalid_id')
        if parent and (_post(db, parent)['parent'] or not _can_read(db, _post(db, parent), actor, moderator)):
            raise SocialError('not_found')
        before = _integer(args.get('before', 2**53-1), 1)
        mode = args.get('feed', 'all')
        if not isinstance(mode, str) or mode not in {'all', 'following', 'saved', 'mine', 'pinned'}:
            raise SocialError('invalid_feed')
        query = _text(args.get('q', ''), 120).casefold()
        muted_tags = {t.casefold() for t in json.loads(person['prefs']).get('muted_tags', [])}
        # Bounded scan with an explicit continuation even when many rows are muted.
        if action != 'thread' and mode in {'saved', 'mine'}:
            rows = db.execute('SELECT * FROM posts WHERE id<? ORDER BY id DESC LIMIT 201', (before,)).fetchall()
        else:
            rows = db.execute('SELECT * FROM posts WHERE parent=? AND id<? ORDER BY id DESC LIMIT 201', (parent, before)).fetchall()
        scanned = 0
        for row in rows[:200]:
            scanned = row['id']
            if not _can_read(db, row, actor, moderator):
                continue
            if mode == 'pinned' and not row['pinned']:
                continue
            if mode == 'mine' and row['author'] != actor:
                continue
            if mode == 'saved' and not _relation(db, actor, 'save', row['id']):
                continue
            if mode == 'following' and row['author'] != actor and not _relation(db, actor, 'follow', row['author']):
                continue
            if query and query not in row['body'].casefold():
                continue
            if muted_tags & {t.casefold() for t in re.findall(r'#(\w+)', row['body'])}:
                continue
            result['posts'].append(_serialize(db, row, actor, moderator))
            if len(result['posts']) >= 20:
                break
        if scanned and any(r['id'] < scanned for r in rows):
            result['next'] = scanned
        if parent:
            result['root'] = _serialize(db, _post(db, parent), actor, moderator)
        return result


def _set_relation(db, actor, kind, target, on):
    if type(on) is not bool:
        raise SocialError('invalid_toggle')
    if on:
        db.execute('INSERT OR IGNORE INTO relations VALUES(?,?,?)', (actor, kind, target))
    else:
        db.execute('DELETE FROM relations WHERE actor=? AND kind=? AND target=?', (actor, kind, target))
