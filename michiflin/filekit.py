"""Per-person temporary file workspace; never exposes host filesystem paths.

Names are virtual SQLite keys, not paths on the host. Immutable content objects
are staged under random IDs, then metadata/history is committed atomically.
The whole workspace is temporary like JOB_ROOT and is not part of state backups.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from . import config, state
from .util import tr


class FileKitError(Exception):
    """Only a short, non-sensitive error code may cross the bridge or logs."""


def error_text(code: str, locale: str) -> str:
    messages = {
        "invalid_name": ("Nombre no válido.", "Invalid name."),
        "invalid_path": ("Ruta no válida.", "Invalid path."),
        "invalid_file": ("Archivo no válido.", "Invalid file."),
        "exists": ("Ya existe un elemento con ese nombre; no se reemplazó nada.", "An item with that name already exists; nothing was replaced."),
        "folder_missing": ("La carpeta ya no existe.", "The folder no longer exists."),
        "file_missing": ("El archivo ya no existe.", "The file no longer exists."),
        "missing": ("El elemento ya no está disponible o no te pertenece.", "The item is unavailable or does not belong to you."),
        "content_missing": ("Los datos ya no están disponibles en este servidor temporal.", "The data is no longer available on this temporary server."),
        "nothing_selected": ("Selecciona al menos un elemento.", "Select at least one item."),
        "overlapping_selection": ("Una carpeta y su contenido no pueden renombrarse a la vez; selecciona solo uno de ellos.",
                                  "A folder and its contents cannot be renamed together; select only one."),
        "inside_source": ("No se puede mover o copiar una carpeta dentro de sí misma.", "A folder cannot be moved or copied inside itself."),
        "source_changed": ("El archivo cambió durante la copia; no se guardó nada.", "The file changed during copying; nothing was saved."),
        "integrity_missing": ("La descarga antigua no tiene huellas suficientes para verificar sus partes; no se copió.", "The older download lacks enough hashes to verify its parts; it was not copied."),
        "join_parts": ("Selecciona partes .001, .002… del mismo archivo y de la misma carpeta.", "Select .001, .002… parts of the same file in the same folder."),
        "join_missing_parts": ("Falta una parte numerada o el conjunto no empieza por .001; no se guardó nada.", "A numbered part is missing or the set does not start at .001; nothing was saved."),
        "invalid_hash": ("La huella SHA-256 debe tener 64 caracteres hexadecimales.", "The SHA-256 fingerprint must contain 64 hexadecimal characters."),
        "join_mismatch": ("Las partes cambiaron o la huella no coincide; no se guardó nada.", "The parts changed or the fingerprint does not match; nothing was saved."),
        "edit_conflict": ("El archivo cambió en otra ventana; vuelve a abrirlo antes de guardar.", "The file changed in another window; reopen it before saving."),
        "text_limit": ("Este texto supera 70.000 bytes; usa una copia local para editarlo.", "This text exceeds 70,000 bytes; use a local copy to edit it."),
        "preview_limit": ("La previsualización remota admite hasta 200.000 bytes; envía una copia al chat para abrir este archivo.", "Remote preview supports up to 200,000 bytes; send a copy to the chat to open this file."),
        "binary_not_text": ("Este archivo no parece texto UTF-8 y no se modificó.", "This file does not appear to be UTF-8 text and was not modified."),
        "disk_full": ("No hay espacio seguro suficiente en el servidor.", "There is not enough safe free space on the server."),
        "input_limit": ("Los archivos superan la memoria o el tamaño disponible para esta operación.", "The files exceed the available memory or size for this operation."),
        "output_limit": ("El contenido supera los límites de tamaño o memoria; no se guardó nada.", "The content exceeds size or memory limits; nothing was saved."),
        "wrong_password": ("Contraseña incorrecta o archivo cifrado incompatible.", "Wrong password or incompatible encrypted archive."),
        "wrong_password_or_corrupt": ("La contraseña es incorrecta o el contenido cifrado está dañado; no se guardó nada.",
                                      "The password is wrong or the encrypted data is damaged; nothing was saved."),
        "archive_failed": ("No se pudo procesar el archivo; comprueba el formato, la contraseña y todas las partes.", "Could not process the archive; check the format, password and all parts."),
        "unsafe_path": ("El comprimido contiene una ruta insegura; no se extrajo.", "The archive contains an unsafe path; nothing was extracted."),
        "unsafe_link": ("El comprimido contiene un enlace no admitido; no se extrajo.", "The archive contains an unsupported link; nothing was extracted."),
        "unsafe_listing": ("No se pudo verificar con seguridad la lista del comprimido.", "The archive listing could not be verified safely."),
        "duplicate_path": ("Hay nombres repetidos; no se reemplazó nada.", "There are duplicate names; nothing was replaced."),
        "listing_limit": ("Hay demasiados elementos o nombres para procesarlos con seguridad.", "There are too many items or names to process safely."),
        "invalid_password": ("La contraseña no es válida.", "The password is invalid."),
        "invalid_volume": ("El tamaño de las partes no es válido.", "Invalid volume size."),
        "missing_archive": ("Selecciona la primera parte y todas sus partes asociadas.", "Select the first volume and all associated parts."),
        "unsupported_format": ("Formato de creación no admitido.", "Unsupported creation format."),
        "engine_unavailable": ("El motor de archivos no está disponible.", "The archive engine is unavailable."),
        "isolation_unavailable": ("El aislamiento del servidor no está disponible; la operación se rechazó.", "Server isolation is unavailable; the operation was refused."),
        "memory_unavailable": ("No hay suficiente memoria segura ahora; inténtalo más tarde.", "There is not enough safe memory now; try again later."),
        "archive_timeout": ("El archivador tardó demasiado y se detuvo sin guardar resultados.", "The archiver timed out and stopped without saving results."),
        "secret_invalid": ("No se pudo proteger la contraseña; inténtalo de nuevo.", "The password could not be protected; try again."),
        "secret_expired": ("El servidor se reinició; actualiza y vuelve a introducir la contraseña.", "The server restarted; refresh and enter the password again."),
        "import_expired": ("La autorización de importación caducó o ya se usó; inicia otra desde el kit.", "The import authorization expired or was already used; start another from the kit."),
        "import_part_limit": ("La copia supera el tamaño configurado por parte; divídela antes de enviarla al bot.", "The copy exceeds the configured per-part size; split it before sending it to the bot."),
        "job_active": ("Espera a que la descarga esté lista o terminada; no se tocaron trabajos activos.", "Wait for the download to be ready or finished; active jobs were not touched."),
        "storage_busy": ("El espacio está ocupado por otra operación; inténtalo más tarde.", "Another operation is using the workspace; try again later."),
        "storage_unavailable": ("El almacenamiento temporal no está disponible.", "Temporary storage is unavailable."),
        "not_authorized": ("No tienes acceso a este espacio.", "You do not have access to this workspace."),
    }
    spanish, english = messages.get(code, ("La operación no se pudo completar; no se reemplazaron archivos.",
                                           "The operation could not be completed; no files were replaced."))
    return tr(locale, spanish, english)


_locks_lock = threading.Lock()
_locks: dict[str, _WorkspaceLock] = {}
_OBJECT = re.compile(r"^[0-9a-f]{32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f/\\]")


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or _CONTROL.search(value):
        raise FileKitError("invalid_name")
    try:
        if len(value.encode("utf-8")) > 255:
            raise FileKitError("invalid_name")
    except UnicodeEncodeError:
        raise FileKitError("invalid_name") from None
    return value


def _path(value: Any, *, root: bool = True) -> str:
    if not isinstance(value, str):
        raise FileKitError("invalid_path")
    if value == "" and root:
        return ""
    if not value.startswith("/") or value.endswith("/") or "//" in value:
        raise FileKitError("invalid_path")
    for part in value[1:].split("/"):
        _name(part)
    return value


def _parent(value: str) -> str:
    return value.rsplit("/", 1)[0]


def _base(value: str) -> str:
    return value.rsplit("/", 1)[-1]


def _child(folder: str, name: str) -> str:
    return _path(folder) + "/" + _name(name)


def _within(value: str, root: str) -> bool:
    return value == root or value.startswith(root + "/")


def _roots(values: Any) -> list[str]:
    if not isinstance(values, list) or not values:
        raise FileKitError("nothing_selected")
    ordered = sorted({_path(value, root=False) for value in values})
    return [value for index, value in enumerate(ordered) if not any(_within(value, other) for other in ordered[:index])]


def _identity(address: str) -> str:
    address = (address or "").strip().lower()
    if not address:
        raise FileKitError("not_authorized")
    # The directory is never a user supplied path and is not a credential.
    return hashlib.sha256(("michiflin-filekit-v1\0" + config.BOT_EPOCH + "\0" + address).encode()).hexdigest()


class _WorkspaceLock:
    """Reentrant in one thread and exclusive across bot processes sharing a disk."""

    def __init__(self, lock_path: Path) -> None:
        self.path = lock_path
        self.mutex = threading.RLock()
        self.local = threading.local()

    def __enter__(self) -> _WorkspaceLock:
        self.mutex.acquire()
        try:
            depth = int(getattr(self.local, "depth", 0))
            if not depth:
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise FileKitError("storage_unavailable")
                    deadline = time.monotonic() + 30
                    while True:
                        state.check_work_allowed()
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise FileKitError("storage_busy") from None
                            time.sleep(0.05)
                    self.local.fd = fd
                except BaseException:
                    os.close(fd)
                    raise
            self.local.depth = depth + 1
            return self
        except BaseException:
            self.mutex.release()
            raise

    def __exit__(self, *_args: Any) -> None:
        depth = self.local.depth - 1
        self.local.depth = depth
        try:
            if not depth:
                fd = self.local.fd
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                del self.local.fd
        finally:
            self.mutex.release()


def _lock(root: Path) -> _WorkspaceLock:
    with _locks_lock:
        return _locks.setdefault(str(root), _WorkspaceLock(root / ".lock"))


def _snapshot(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {row["path"]: dict(row) for row in db.execute("SELECT path,type,blob,size,mime,mtime FROM entries")}


def _history(db: sqlite3.Connection) -> dict[str, list]:
    row = db.execute("SELECT value FROM meta WHERE key='history'").fetchone()
    return json.loads(row[0]) if row else {"past": [], "future": []}


def _directory(all_entries: dict[str, dict[str, Any]], folder: str) -> None:
    _path(folder)
    if folder and (folder not in all_entries or all_entries[folder]["type"] != "dir"):
        raise FileKitError("folder_missing")


def _apply(db: sqlite3.Connection, before: list[dict], after: list[dict]) -> None:
    for path in {entry["path"] for entry in before + after}:
        db.execute("DELETE FROM entries WHERE path=?", (path,))
    for item in after:
        db.execute("INSERT INTO entries(path,type,blob,size,mime,mtime) VALUES (?,?,?,?,?,?)",
                   (item["path"], item["type"], item.get("blob"), item.get("size", 0), item.get("mime", ""), item.get("mtime", 0)))


def _event(db: sqlite3.Connection, label: str, before: list[dict], after: list[dict]) -> None:
    if not before and not after:
        return
    _apply(db, before, after)
    history = _history(db)
    history["past"].append({"label": label, "before": before, "after": after})
    history["future"] = []
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('history',?)", (json.dumps(history, ensure_ascii=False, separators=(",", ":")),))


class Workspace:
    """The caller must derive `address` from the authenticated chat, not request arguments."""

    def __init__(self, address: str, root: Path | None = None) -> None:
        self.identity = _identity(address)
        self.root = (root or config.JOB_ROOT / "_filekit") / self.identity
        self.objects = self.root / "objects"
        for directory in (self.root.parent, self.root, self.objects):
            if directory.is_symlink():
                raise FileKitError("storage_unavailable")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink():
                raise FileKitError("storage_unavailable")
            directory.chmod(0o700)
        self.lock = _lock(self.root)
        with self.lock:
            self.database = self.root / "index.sqlite3"
            if self.database.is_symlink():
                raise FileKitError("storage_unavailable")
            with self._db() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS entries(
                        path TEXT PRIMARY KEY, type TEXT NOT NULL, blob TEXT,
                        size INTEGER NOT NULL, mime TEXT NOT NULL, mtime REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """)
            self.database.chmod(0o600)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _object(self, value: str) -> Path:
        if not isinstance(value, str) or not _OBJECT.fullmatch(value):
            raise FileKitError("content_missing")
        path = self.objects / value
        if path.is_symlink() or not path.is_file():
            raise FileKitError("content_missing")
        return path

    def view(self) -> dict[str, Any]:
        with self.lock, self._db() as db:
            all_entries = _snapshot(db)
            history = _history(db)
            entries = [{key: value for key, value in item.items() if key != "blob"} for item in all_entries.values()]
            return {"entries": entries, "canUndo": bool(history["past"]), "canRedo": bool(history["future"])}

    def content(self, path: str) -> tuple[dict[str, Any], Path]:
        path = _path(path, root=False)
        with self.lock, self._db() as db:
            item = _snapshot(db).get(path)
            if not item or item["type"] != "file":
                raise FileKitError("file_missing")
            return item, self._object(item["blob"])

    def _stage(self, source: BinaryIO, combined: Any = None) -> tuple[str, int, str]:
        """A failed/paused copy never publishes a partial file."""
        identity = uuid.uuid4().hex
        temporary = self.objects / (identity + ".part")
        final = self.objects / identity
        count = 0
        digest = hashlib.sha256()
        try:
            with temporary.open("xb") as target:
                os.chmod(temporary, 0o600)
                while True:
                    state.check_work_allowed()
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    count += len(block)
                    if count > config.MAX_FILE_BYTES:
                        raise FileKitError("input_limit")
                    if shutil.disk_usage(self.root).free < config.MIN_FREE_DISK_BYTES:
                        raise FileKitError("disk_full")
                    digest.update(block)
                    if combined is not None:
                        combined.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            state.check_work_allowed()
            # O_EXCL-equivalent publication: an unlikely ID collision cannot overwrite
            # an existing immutable object.
            os.link(temporary, final, follow_symlinks=False)
            temporary.unlink()
            return identity, count, digest.hexdigest()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _transaction(self, label: str, plan: Any) -> None:
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            before, after = plan(_snapshot(db))
            state.check_work_allowed()
            _event(db, label, before, after)
            db.commit()

    def mkdir(self, folder: str, filename: str) -> None:
        target = _child(folder, filename)
        def plan(all_entries: dict[str, dict]) -> tuple[list, list]:
            _directory(all_entries, folder)
            if target in all_entries:
                raise FileKitError("exists")
            return [], [{"path": target, "type": "dir", "blob": None, "size": 0, "mime": "", "mtime": time.time()}]
        self._transaction("mkdir", plan)

    def put(self, folder: str, sources: list[dict[str, Any]], *, expected_sha256: str = "", expected_size: int | None = None) -> None:
        """Import streams atomically; optional aggregate integrity covers bytes staged in order."""
        folder = _path(folder)
        if not isinstance(sources, list) or not sources:
            raise FileKitError("nothing_selected")
        if expected_sha256 and (not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)):
            raise FileKitError("source_changed")
        if expected_size is not None and (type(expected_size) is not int or expected_size < 0):
            raise FileKitError("source_changed")
        prepared = []
        for item in sources:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise FileKitError("invalid_file")
            parts = item["path"].split("/")
            for part in parts:
                _name(part)
            if item.get("type") != "dir" and not hasattr(item.get("source"), "read"):
                raise FileKitError("invalid_file")
            prepared.append({**item, "parts": parts})
        staged: list[str] = []
        combined = hashlib.sha256() if expected_sha256 else None
        total = 0
        with self.lock:
            try:
                for item in prepared:
                    if item.get("type") != "dir":
                        item["blob"], item["size"], digest = self._stage(item["source"], combined)
                        staged.append(item["blob"])
                        total += item["size"]
                        expected = item.get("sha256")
                        if expected and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected) or digest != expected.lower()):
                            raise FileKitError("source_changed")
                        if item.get("expected_size") is not None and item["size"] != item["expected_size"]:
                            raise FileKitError("source_changed")
                if (combined is not None and combined.hexdigest() != expected_sha256.lower()) or (expected_size is not None and total != expected_size):
                    raise FileKitError("source_changed")
                def plan(all_entries: dict[str, dict]) -> tuple[list, list]:
                    _directory(all_entries, folder)
                    known = dict(all_entries)
                    added: list[dict] = []
                    for item in prepared:
                        at = folder
                        for part in item["parts"][:-1]:
                            at = _child(at, part)
                            existing = known.get(at)
                            if existing and existing["type"] != "dir":
                                raise FileKitError("exists")
                            if not existing:
                                directory = {"path": at, "type": "dir", "blob": None, "size": 0, "mime": "", "mtime": time.time()}
                                known[at] = directory
                                added.append(directory)
                        target = _child(at, item["parts"][-1])
                        existing = known.get(target)
                        if existing:
                            if item.get("type") == "dir" and existing["type"] == "dir" and existing in added:
                                continue
                            raise FileKitError("exists")
                        is_dir = item.get("type") == "dir"
                        entry = {"path": target, "type": "dir" if is_dir else "file", "blob": None if is_dir else item["blob"],
                                 "size": 0 if is_dir else item["size"],
                                 "mime": "" if is_dir else str(item.get("mime") or mimetypes.guess_type(target)[0] or "application/octet-stream"),
                                 "mtime": float(item.get("mtime") or time.time())}
                        known[target] = entry
                        added.append(entry)
                    return [], added
                self._transaction("import", plan)
            except BaseException:
                for identity in staged:
                    (self.objects / identity).unlink(missing_ok=True)
                raise

    def write(self, path: str, source: BinaryIO, mime: str = "text/plain", expected_blob: str | None = None) -> None:
        path = _path(path, root=False)
        with self.lock:
            identity, size, _digest = self._stage(source)
            try:
                def plan(all_entries: dict[str, dict]) -> tuple[list, list]:
                    old = all_entries.get(path)
                    if not old or old["type"] != "file":
                        raise FileKitError("file_missing")
                    if expected_blob is not None and old["blob"] != expected_blob:
                        raise FileKitError("edit_conflict")
                    return [old], [{**old, "blob": identity, "size": size, "mime": mime or old["mime"], "mtime": time.time()}]
                self._transaction("edit", plan)
            except BaseException:
                (self.objects / identity).unlink(missing_ok=True)
                raise

    @staticmethod
    def _relocate(all_entries: dict[str, dict], values: list[str], folder: str, copy: bool,
                  targets: dict[str, str] | None = None) -> tuple[list, list]:
        _directory(all_entries, folder)
        taken = set(all_entries)
        before, after = [], []
        for source in _roots(values):
            if source not in all_entries:
                raise FileKitError("missing")
            target = (targets or {}).get(source) or _child(folder, _base(source))
            if _within(target, source):
                raise FileKitError("inside_source")
            subtree = [item for path, item in all_entries.items() if _within(path, source)]
            for item in subtree:
                new_path = target + item["path"][len(source):]
                _path(new_path, root=False)
                if new_path in taken:
                    raise FileKitError("exists")
                taken.add(new_path)
                after.append({**item, "path": new_path})
            if not copy:
                before.extend(subtree)
        return before, after

    def rename(self, path: str, filename: str) -> None:
        path = _path(path, root=False)
        folder = _parent(path)
        target = _child(folder, filename)
        if target == path:
            return
        self._transaction("rename", lambda all_entries: self._relocate(all_entries, [path], folder, False, {path: target}))

    def rename_many(self, changes: list[dict[str, str]]) -> None:
        """Rename disjoint roots in one transaction, including atomic name swaps.

        Existing descendants move with a directory. Reject ambiguous nested
        selections and collisions before touching any entry or history.
        """
        if not isinstance(changes, list) or not changes or len(changes) > 1000:
            raise FileKitError("nothing_selected")
        targets: dict[str, str] = {}
        for item in changes:
            if not isinstance(item, dict):
                raise FileKitError("invalid_request")
            source = _path(item.get("path"), root=False)
            target = _child(_parent(source), item.get("name"))
            if source in targets:
                raise FileKitError("duplicate_path")
            targets[source] = target
        if len(_roots(list(targets))) != len(targets):
            raise FileKitError("overlapping_selection")

        def plan(all_entries: dict[str, dict]) -> tuple[list, list]:
            if any(source not in all_entries for source in targets):
                raise FileKitError("missing")
            changed = {source: target for source, target in targets.items() if source != target}
            if not changed:
                return [], []
            before = [entry for path, entry in all_entries.items() if any(_within(path, source) for source in changed)]
            taken = set(all_entries).difference(entry["path"] for entry in before)
            after = []
            for source, target in changed.items():
                for entry in before:
                    if not _within(entry["path"], source):
                        continue
                    next_path = _path(target + entry["path"][len(source):], root=False)
                    if next_path in taken:
                        raise FileKitError("exists")
                    taken.add(next_path)
                    after.append({**entry, "path": next_path})
            return before, after
        self._transaction("rename_many", plan)

    def transfer(self, values: list[str], folder: str, copy: bool) -> None:
        folder = _path(folder)
        self._transaction("copy" if copy else "move", lambda all_entries: self._relocate(all_entries, values, folder, copy))

    def remove(self, values: list[str]) -> None:
        roots = _roots(values)
        def plan(all_entries: dict[str, dict]) -> tuple[list, list]:
            if any(value not in all_entries for value in roots):
                raise FileKitError("missing")
            return [item for path, item in all_entries.items() if any(_within(path, root) for root in roots)], []
        self._transaction("delete", plan)

    def travel(self, reverse: bool) -> bool:
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            history = _history(db)
            source, destination = (history["past"], history["future"]) if reverse else (history["future"], history["past"])
            if not source:
                db.commit()
                return False
            event = source.pop()
            _apply(db, event["after"] if reverse else event["before"], event["before"] if reverse else event["after"])
            destination.append(event)
            state.check_work_allowed()
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('history',?)", (json.dumps(history, ensure_ascii=False, separators=(",", ":")),))
            db.commit()
            return True

    def clear_history(self) -> None:
        """Explicitly discard undo history and collect no-longer-referenced objects."""
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            live = {item["blob"] for item in _snapshot(db).values() if item["type"] == "file"}
            state.check_work_allowed()
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('history',?)", ('{"past":[],"future":[]}',))
            db.commit()
            # Do not touch .part files or anything not created as a content object.
            for item in self.objects.iterdir():
                if _OBJECT.fullmatch(item.name) and item.name not in live and item.is_file() and not item.is_symlink():
                    item.unlink()


def open_source(path: Path) -> BinaryIO:
    """Open one regular, non-symlink file for copying; never delete or modify it."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise FileKitError("invalid_file")
        return os.fdopen(fd, "rb")
    except OSError:
        raise FileKitError("content_missing") from None
