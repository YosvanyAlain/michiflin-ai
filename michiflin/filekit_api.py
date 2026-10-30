"""Authorized WebXDC bridge operations for the temporary remote file kit.

The enclosing miniapp checks instance, sender, grant, identity epoch and live
session. Passwords and file bytes use process- or request-ephemeral envelopes;
metadata such as file names remains visible to the encrypted chat's updates.
"""
from __future__ import annotations

import io
import json
from typing import Any

from . import chat, config, filekit, filekit_archive, filekit_crypto, filekit_join_remote, filekit_rar_native, filekit_transfer, mail_profiles, queue, state

_PREVIEW_BYTES = 200_000
_TEXT_BYTES = 70_000


class DeliveryRefused(Exception):
    """Already-localized queue limit, without including user file contents."""


def _string(args: dict[str, Any], key: str, default: str = "", limit: int = 4096) -> str:
    value = args.get(key, default)
    if not isinstance(value, str) or len(value) > limit:
        raise filekit.FileKitError("invalid_path")
    return value


def _selected(args: dict[str, Any]) -> list[str]:
    values = args.get("paths", [])
    if not isinstance(values, list) or len(values) > 1000 or any(not isinstance(v, str) or len(v) > 4096 for v in values):
        raise filekit.FileKitError("nothing_selected")
    return values


def _password(args: dict[str, Any]) -> str:
    if "password" in args:  # Never accept plaintext passwords in persisted updates.
        raise filekit.FileKitError("invalid_password")
    return filekit_crypto.decrypt(args["secret"]) if args.get("secret") else ""


def _text_bytes(raw: bytes) -> str:
    if b"\x00" in raw:
        raise filekit.FileKitError("binary_not_text")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise filekit.FileKitError("binary_not_text") from None


def _read(space: filekit.Workspace, path: str, max_bytes: int) -> tuple[dict[str, Any], bytes]:
    with space.lock:
        entry, blob = space.content(path)
        if int(entry["size"]) > max_bytes:
            raise filekit.FileKitError("preview_limit" if max_bytes == _PREVIEW_BYTES else "text_limit")
        try:
            with filekit.open_source(blob) as source:
                result = source.read(max_bytes + 1)
        except FileNotFoundError:
            raise filekit.FileKitError("content_missing") from None
        state.check_work_allowed()
        if len(result) != entry["size"] or len(result) > max_bytes:
            raise filekit.FileKitError("source_changed")
        return entry, result


def dispatch(chat_id: int, args: dict[str, Any], locale: str) -> dict[str, Any]:
    sender = chat.sender()
    if not sender or not isinstance(args, dict):
        raise filekit.FileKitError("not_authorized")
    action = _string(args, "action", limit=40)
    space = filekit.Workspace(sender)
    if action == "list":
        view = space.view()
        settings = mail_profiles.effective_settings(sender, {"part_mb": int(state.setting("part_mb"))})
        part_mib = min(settings["part_mb"], 10) if state.get_nauta_strict(sender) else settings["part_mb"]
        return {**view, "key": filekit_crypto.public_key(), "previewBytes": _PREVIEW_BYTES,
                "textBytes": _TEXT_BYTES, "partMiB": part_mib,
                "storage": "temporary", "archive": "sandbox_required", "rarNative": filekit_rar_native.available()}
    if action == "mkdir":
        space.mkdir(_string(args, "folder", ""), _string(args, "name", limit=255))
    elif action == "rename":
        space.rename(_string(args, "path"), _string(args, "name", limit=255))
    elif action == "rename_many":
        changes = args.get("changes")
        if not isinstance(changes, list) or not changes or len(changes) > 1000:
            raise filekit.FileKitError("nothing_selected")
        if any(not isinstance(item, dict) or not isinstance(item.get("path"), str) or len(item["path"]) > 4096
               or not isinstance(item.get("name"), str) or len(item["name"]) > 255 for item in changes):
            raise filekit.FileKitError("invalid_name")
        space.rename_many(changes)
    elif action in ("copy", "move"):
        space.transfer(_selected(args), _string(args, "folder", ""), copy=(action == "copy"))
    elif action == "delete":
        space.remove(_selected(args))
    elif action in ("undo", "redo"):
        return {"changed": space.travel(action == "undo")}
    elif action == "clear_history":
        space.clear_history()
    elif action == "prepare_import":
        size = args.get("size")
        if type(size) is not int:
            raise filekit.FileKitError("invalid_file")
        settings = mail_profiles.effective_settings(sender, {"part_mb": int(state.setting("part_mb"))})
        part_mib = min(settings["part_mb"], 10) if state.get_nauta_strict(sender) else settings["part_mb"]
        if size > part_mib * config.MIB:
            raise filekit.FileKitError("import_part_limit")
        digest = filekit_crypto.decrypt(args.get("secret"))
        return filekit_transfer.prepare_import(sender, _string(args, "folder", ""),
                                               _string(args, "name", limit=255), size, digest)
    elif action == "import_job":
        filekit_transfer.import_job(space, _string(args, "job", limit=128),
                                    _string(args, "folder", ""), _string(args, "name", limit=255))
        return {"changed": True}
    elif action == "read":
        entry, raw = _read(space, _string(args, "path"), _PREVIEW_BYTES)
        return {"sealed": filekit_crypto.encrypt_reply(args.get("replyKey"), raw),
                "size": entry["size"], "mime": entry["mime"], "version": entry["blob"]}
    elif action == "write":
        path = _string(args, "path")
        expected = _string(args, "version", limit=128)
        if not expected:
            raise filekit.FileKitError("edit_conflict")
        entry, old = _read(space, path, _TEXT_BYTES)
        _text_bytes(old)  # Refuse to reinterpret arbitrary binary as editable text.
        content = filekit_crypto.decrypt_text(args.get("sealed"))
        raw = content.encode("utf-8")
        if len(raw) > _TEXT_BYTES:
            raise filekit.FileKitError("text_limit")
        _text_bytes(raw)
        space.write(path, io.BytesIO(raw), entry["mime"], expected_blob=expected)
    elif action == "send":
        path = _string(args, "path")
        part = args.get("partMiB")
        if part in (None, ""):
            part = None
        elif type(part) is not int or part < 1 or part > 20:
            raise filekit.FileKitError("invalid_volume")
        with space.lock:
            entry, blob = space.content(path)
            try:
                with filekit.open_source(blob) as source:
                    job, error = queue.enqueue_local(chat_id, source, entry["path"].rsplit("/", 1)[-1],
                                                     entry["size"], entry["mime"], locale, part)
            except ValueError:
                raise filekit.FileKitError("source_changed") from None
            if not job:
                if error:
                    raise DeliveryRefused(error)
                raise filekit.FileKitError("invalid_file")
        return {"job": job["id"]}
    elif action == "join_raw":
        try:
            protected = json.loads(filekit_crypto.decrypt_text(args.get("sealed")))
        except ValueError:
            raise filekit.FileKitError("invalid_hash") from None
        if not isinstance(protected, dict):
            raise filekit.FileKitError("invalid_hash")
        filekit_join_remote.join(space, _selected(args), folder=_string(args, "folder", ""),
                                 name=_string(args, "name", limit=255), sha256=protected.get("sha256", ""))
        return {"changed": True}
    elif action == "rar_list":
        password = _password(args)
        try:
            result = filekit_rar_native.inspect(space, _selected(args), _string(args, "first"), password)
            raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(raw) > 300_000:
                raise filekit.FileKitError("listing_limit")
            # Member names, sizes and the version token are not persisted in clear.
            return {"sealed": filekit_crypto.encrypt_reply(args.get("replyKey"), raw)}
        finally:
            password = ""
    elif action == "rar_extract":
        password = _password(args)
        try:
            # Selection and version travel in an encrypted envelope, just like
            # the password. Never accept a cleartext member list as a fallback.
            payload = filekit_crypto.decrypt_text(args.get("sealed"))
            try:
                selection = json.loads(payload)
            except ValueError:
                raise filekit.FileKitError("invalid_path") from None
            if not isinstance(selection, dict):
                raise filekit.FileKitError("invalid_path")
            filekit_rar_native.extract(space, _selected(args), _string(args, "first"), password,
                                       selection.get("members"), selection.get("version"),
                                       folder=_string(args, "folder", ""), name=_string(args, "name", limit=255))
        finally:
            password = ""
        return {"changed": True}
    elif action == "archive_create":
        fmt = _string(args, "format", limit=8)
        folder = _string(args, "folder", "")
        output_name = _string(args, "name", limit=255)
        volume = args.get("volumeBytes", 0)
        if type(volume) is not int or volume < 0 or volume > 512 * config.MIB:
            raise filekit.FileKitError("invalid_volume")
        password = _password(args)
        try:
            filekit_archive.archive(space, "create", _selected(args), folder=folder, name=output_name,
                                    format=fmt, password=password, volume_bytes=volume)
        finally:
            password = ""  # Python strings cannot be reliably zeroed; never persist or log them.
        return {"changed": True}
    elif action == "archive_extract":
        password = _password(args)
        try:
            filekit_archive.archive(space, "extract", _selected(args), first=_string(args, "first"),
                                    folder=_string(args, "folder", ""), name=_string(args, "name", limit=255), password=password)
        finally:
            password = ""
        return {"changed": True}
    else:
        raise filekit.FileKitError("invalid_path")
    return {"changed": True}
