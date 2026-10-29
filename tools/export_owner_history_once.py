#!/usr/bin/env python3
"""Separate recoverable owner data from a retired Delta Chat identity, once.

The output is an encrypted, private-repository archive of selected message
content, WebXDC status updates, available attachments, and owner/ambiguous app
state. It is deliberately NOT a Delta Chat account backup: it excludes account
configuration, keypairs, transport passwords, QR tokens and server bindings.
It is not a migration of private, per-device WebXDC storage. Old backups remain
untouched. Every uploaded byte is authenticated and independently recovered
before the new account records the archive pointer.
"""
from __future__ import annotations

import base64
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tarfile
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests  # noqa: E402
import state_backup  # noqa: E402
from michiflin import purge_users  # noqa: E402
from tools import cutover_identity_once as cutover, migrate_relay_once as relay  # noqa: E402

MARKER = ".owner-history-export-v1.json"
PREFIX = "michiflin-owner-history-only-"
MSG_COLUMNS = ("id", "rfc724_mid", "chat_id", "from_id", "to_id", "timestamp", "type", "state", "msgrmsg", "bytes",
               "txt", "txt_raw", "param", "starred", "timestamp_sent", "timestamp_rcvd", "hidden", "error",
               "ephemeral_timer", "ephemeral_timestamp", "subject", "mime_in_reply_to", "mime_references", "mime_modified",
               "download_state", "deleted", "pinned")
CHAT_COLUMNS = ("id", "type", "name", "draft_timestamp", "draft_txt", "archived", "created_timestamp",
                "muted_until", "ephemeral_timer", "protected")
UPDATE_COLUMNS = ("id", "msg_id", "update_item", "uid")
README = ("Michiflin: contenido recuperable de la identidad anterior.\n"
          "No es una cuenta de Delta Chat ni contiene su configuración o claves.\n"
          "Los mensajes y actualizaciones conservan identificadores locales históricos;\n"
          "no pueden reactivar chats, invitaciones ni trabajos de la identidad antigua.\n"
          "Los nombres de archivos en adjuntos.json se corresponden con archivos/;\n"
          "los ausentes ya no estaban en la copia examinada. El almacenamiento local\n"
          "de cada instancia WebXDC en el dispositivo no se incluye.\n").encode("utf-8")


class ExportError(RuntimeError):
    pass


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"encoding": "base64", "value": base64.b64encode(value).decode("ascii")}
    return value


def _rows(db: Any, table: str, columns: tuple[str, ...], key: str, ids: set[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    if not re.fullmatch(r"[a-z][a-z0-9_]*", table) or not all(re.fullmatch(r"[a-z][a-z0-9_]*", x) for x in columns + (key,)):
        raise ExportError("unexpected schema identifier")
    present = {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}
    if not set(columns + (key,)).issubset(present):
        raise ExportError("unsupported account database schema")
    # Chunks prevent SQLite's bound-variable limit from truncating a long history.
    result = []
    ordered = sorted(ids)
    for start in range(0, len(ordered), 500):
        chunk = ordered[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        fields = ",".join('"' + x + '"' for x in columns)
        rows = db.execute(f'SELECT {fields} FROM "{table}" WHERE "{key}" IN ({marks})', chunk)
        result.extend({name: _cell(value) for name, value in zip(columns, row)} for row in rows)
    return sorted(result, key=lambda item: int(item["id"]))


def _secret_patterns(db: Any) -> set[bytes]:
    """Identify the old account's own keys, passwords and capability tokens."""
    raw: list[Any] = [row[0] for row in db.execute("SELECT private_key FROM keypairs")]
    for key, value in db.execute("SELECT keyname, value FROM config"):
        if any(part in str(key).lower() for part in ("password", "passwd", "secret", "token", "_pw")):
            raw.append(value)
    for first, second in db.execute("SELECT entered_param, configured_param FROM transports"):
        for text in (first, second):
            for line in str(text or "").splitlines():
                key, separator, value = line.partition("=")
                if separator and any(part in key.lower() for part in ("password", "passwd", "secret", "token", "_pw")):
                    raw.append(value)
    raw.extend(row[0] for row in db.execute("SELECT token FROM tokens"))
    patterns = set()
    for item in raw:
        if item is None:
            continue
        value = item if isinstance(item, bytes) else str(item).encode("utf-8")
        # Extremely short generic strings would corrupt ordinary message text.
        if len(value) < 12:
            continue
        patterns.add(value)
        if len(value) >= 64:
            patterns.add(base64.b64encode(value))
    return patterns


def _redact(value: Any, patterns: set[bytes]) -> tuple[Any, int]:
    """Remove exact old account secrets if they occur in preserved text/state."""
    count = 0
    if isinstance(value, str):
        for pattern in sorted(patterns, key=len, reverse=True):
            try:
                text = pattern.decode("utf-8")
            except UnicodeError:
                continue
            occurrences = value.count(text)
            if occurrences:
                value = value.replace(text, "[credencial de identidad anterior retirada]")
                count += occurrences
        return value, count
    if isinstance(value, list):
        clean = []
        for item in value:
            fixed, n = _redact(item, patterns)
            clean.append(fixed)
            count += n
        return clean, count
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            fixed_key, n = _redact(str(key), patterns)
            fixed, m = _redact(item, patterns)
            if fixed_key in clean:
                raise ExportError("secret redaction would merge two data keys")
            clean[fixed_key] = fixed
            count += n + m
        return clean, count
    return value, 0


def _attachment_names(db: Any, chats: set[int]) -> set[str]:
    names = set()
    for row in _rows(db, "msgs", ("id", "param"), "chat_id", chats):
        for line in str(row.get("param") or "").splitlines():
            prefix = "f=$BLOBDIR/"
            if line.startswith(prefix):
                name = line[len(prefix):]
                if not name or Path(name).name != name or "\x00" in name:
                    raise ExportError("unsafe attachment reference")
                names.add(name)
    return names


def _build_parts(path: Path, owner: str, extra_patterns: set[bytes] | None = None) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Collect owner and ambiguous records; never query another person's direct chat."""
    owner = str(owner or "").strip().casefold()
    if not purge_users.EMAIL.fullmatch(owner):
        raise ExportError("owner missing")
    state = purge_users._load(path)
    with relay.account_rpc(path / "accounts") as rpc:
        account = relay.only_account(rpc)
        bot = str(rpc.get_config(account, "addr") or "").strip().casefold()
        other, retained, _ = purge_users._classify(rpc, account, owner, bot)
        msg_ids = purge_users._message_ids(rpc, account, retained)
        other_ids = purge_users._message_ids(rpc, account, other)
        projection, _ = purge_users._owner_projection(state, owner, other, other_ids)
        # Projection is read-only and must not masquerade as a new cleanup run.
        original_flags = state.get("flags", {})
        if purge_users.MARKER not in original_flags:
            projection.get("flags", {}).pop(purge_users.MARKER, None)
        blob = Path(rpc.get_blob_dir(account)).resolve(strict=True)
        with closing(purge_users._db(rpc, account)) as db:
            patterns = _secret_patterns(db) | (extra_patterns or set())
            chats = _rows(db, "chats", CHAT_COLUMNS, "id", retained)
            messages = _rows(db, "msgs", MSG_COLUMNS, "chat_id", retained)
            if {int(x["id"]) for x in messages} != msg_ids:
                raise ExportError("message coverage differs from source")
            updates = _rows(db, "msgs_status_updates", UPDATE_COLUMNS, "msg_id", msg_ids)
            referenced = _attachment_names(db, retained)
        documents, redactions = {}, 0
        for name, data in (("conversaciones.json", chats), ("mensajes.json", messages),
                           ("actualizaciones-webxdc.json", updates), ("estado-del-dueno.json", projection)):
            safe, count = _redact(data, patterns)
            redactions += count
            documents[name] = _json(safe)
        present, missing, files = [], [], {}
        for name in sorted(referenced):
            file = blob / name
            if file.is_symlink() or not file.is_file():
                missing.append(name)
                continue
            resolved = file.resolve(strict=True)
            if blob not in resolved.parents:
                raise ExportError("attachment escapes blob directory")
            data = resolved.read_bytes()
            # Never silently discard a whole owner file containing an account secret.
            if any(secret in data for secret in patterns):
                raise ExportError("an attachment contains an old account secret; no backup was removed")
            digest = hashlib.sha256(data).hexdigest()
            target = "archivos/" + digest + ".bin"
            files[target] = data
            present.append({"nombre_original": name, "archivo": target, "sha256": digest, "bytes": len(data)})
        attachments, n = _redact({"presentes": present, "ausentes": missing}, patterns)
        redactions += n
        documents["adjuntos.json"] = _json(attachments)
        documents["LEEME.txt"] = README
        documents.update(files)
        for name, data in documents.items():
            if any(secret in data for secret in patterns):
                raise ExportError("a prior account secret remains in selected output")
        counts = {"chats": len(chats), "messages": len(messages), "updates": len(updates),
                  "attachments_present": len(present), "attachments_missing": len(missing),
                  "redacted_old_secrets": redactions}
        return documents, counts


def pack(parts: dict[str, bytes], counts: dict[str, Any]) -> bytes:
    manifest = {"version": 1, "kind": "owner-history-without-account", "counts": counts,
                "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                          for name, data in sorted(parts.items())}}
    contents = {**parts, "manifiesto.json": _json(manifest)}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, data in sorted(contents.items()):
            if not re.fullmatch(r"(?:[a-zA-Z0-9_-]+\.json|LEEME\.txt|archivos/[0-9a-f]{64}\.bin|revisiones/[0-9a-f]{64}\.json)", name):
                raise ExportError("unsafe export filename")
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, 0
            tar.addfile(info, io.BytesIO(data))
    result = output.getvalue()
    verify(result, expected=parts, expected_counts=counts)
    return result


def verify(raw: bytes, *, expected: dict[str, bytes] | None = None,
           expected_counts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify coverage, hashes and schema without extracting files to the filesystem."""
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        members = tar.getmembers()
        names = [item.name for item in members]
        if len(names) != len(set(names)) or any(not item.isfile() or item.size > state_backup.MAX_BACKUP_BYTES for item in members):
            raise ExportError("invalid export members")
        if not all(re.fullmatch(r"(?:[a-zA-Z0-9_-]+\.json|LEEME\.txt|archivos/[0-9a-f]{64}\.bin|revisiones/[0-9a-f]{64}\.json)", name) for name in names):
            raise ExportError("unsafe export member")
        content = {item.name: tar.extractfile(item).read() for item in members}
    if "manifiesto.json" not in content:
        raise ExportError("export manifest missing")
    manifest = json.loads(content.pop("manifiesto.json"))
    if manifest.get("version") != 1 or manifest.get("kind") != "owner-history-without-account":
        raise ExportError("unexpected export format")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(content):
        raise ExportError("export coverage mismatch")
    for name, data in content.items():
        if files[name] != {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}:
            raise ExportError("export content mismatch")
    if expected is not None and content != expected:
        raise ExportError("source and recovered export differ")
    if expected_counts is not None and manifest.get("counts") != expected_counts:
        raise ExportError("source and recovered counts differ")
    return manifest


def _patterns_from_path(path: Path) -> set[bytes]:
    databases = list((path / "accounts").rglob("dc.db"))
    if len(databases) != 1:
        raise ExportError("unexpected account database count")
    uri = databases[0].resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        return _secret_patterns(db)


def collect_historical_sources(storage: cutover.ReleaseStorage, root: Path, old_fingerprint: str) -> list[Path]:
    """Read every verifiable old routine snapshot and Git revision; do not delete them."""
    paths = []
    assets = state_backup._assets(storage.token, storage.repo, storage.release["id"])
    for item in assets:
        if not state_backup._managed(item):
            continue
        raw = state_backup.decrypt_backup(storage.download(item["id"]), storage.password)
        state_backup._verify_plaintext(raw)
        dest = root / ("routine-" + str(item["id"]))
        state_backup._install_restored(raw, dest)
        if relay.identity(dest)["fingerprint"] == old_fingerprint:
            paths.append(dest)
    url = "https://api.github.com/repos/" + storage.repo + "/commits"
    response = requests.get(url, headers=state_backup.headers(storage.token),
                            params={"path": os.getenv("STATE_BACKUP_PATH", "michiflin-state.bin"), "per_page": 100}, timeout=60)
    response.raise_for_status()
    if 'rel="next"' in response.headers.get("Link", ""):
        raise ExportError("additional historical Git revisions need review")
    revisions = response.json()
    if not isinstance(revisions, list):
        raise ExportError("historical Git revisions unavailable")
    previous_config, previous_branch = os.environ.get("CONFIG_DIR"), os.environ.get("STATE_BACKUP_BRANCH")
    try:
        for index, item in enumerate(revisions):
            sha = item.get("sha") if isinstance(item, dict) else None
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ExportError("invalid historical Git revision")
            dest = root / ("git-" + str(index))
            os.environ["CONFIG_DIR"], os.environ["STATE_BACKUP_BRANCH"] = str(dest), sha
            with redirect_stdout(io.StringIO()):
                state_backup._restore_legacy()
            # Rewriting the private Git chain must not discard a different identity.
            if relay.identity(dest)["fingerprint"] != old_fingerprint:
                raise ExportError("a historical Git revision has a different identity")
            paths.append(dest)
    finally:
        for key, value in (("CONFIG_DIR", previous_config), ("STATE_BACKUP_BRANCH", previous_branch)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return paths


def merge_historical_parts(base: dict[str, bytes], counts: dict[str, Any],
                           history: list[tuple[dict[str, bytes], dict[str, Any]]]) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Keep unique owner records and attachment versions from every reviewed snapshot."""
    result = dict(base)
    index = []
    message_versions: set[str] = set()
    update_versions: set[str] = set()
    for source_parts, source_counts in [(base, counts), *history]:
        docs = {}
        for name, data in source_parts.items():
            if name.startswith("archivos/"):
                if name in result and result[name] != data:
                    raise ExportError("attachment digest collision")
                result[name] = data
            elif name.endswith(".json"):
                if name in base and base[name] == data:
                    docs[name] = name
                else:
                    reference = "revisiones/" + hashlib.sha256(data).hexdigest() + ".json"
                    if reference in result and result[reference] != data:
                        raise ExportError("history digest collision")
                    result[reference] = data
                    docs[name] = reference
        for row in json.loads(source_parts["mensajes.json"]):
            message_versions.add(hashlib.sha256(_json(row)).hexdigest())
        for row in json.loads(source_parts["actualizaciones-webxdc.json"]):
            update_versions.add(hashlib.sha256(_json(row)).hexdigest())
        index.append({"documentos": docs, "conteos": source_counts})
    result["indice-historico.json"] = _json({"version": 1, "revisiones": index})
    combined = {**counts, "source_versions": len(index), "message_versions": len(message_versions),
                "update_versions": len(update_versions),
                "attachment_versions": sum(name.startswith("archivos/") for name in result)}
    return result, combined


def verify_sources_covered(raw: bytes, sources: list[tuple[dict[str, bytes], dict[str, Any]]]) -> dict[str, Any]:
    """Verify every still-present source has a matching full revision and its attachments."""
    manifest = verify(raw)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        content = {item.name: tar.extractfile(item).read() for item in tar.getmembers()}
    index = json.loads(content["indice-historico.json"])
    revisions = index.get("revisiones") if isinstance(index, dict) else None
    if not isinstance(revisions, list) or not revisions or len(revisions) != manifest.get("counts", {}).get("source_versions"):
        raise ExportError("invalid historical export index")
    for parts, counts in sources:
        for name, data in parts.items():
            if name.startswith("archivos/") and content.get(name) != data:
                raise ExportError("historical attachment not covered")
        json_parts = {name: data for name, data in parts.items() if name.endswith(".json")}
        def covers(revision: Any) -> bool:
            if not isinstance(revision, dict) or revision.get("conteos") != counts:
                return False
            docs = revision.get("documentos")
            return isinstance(docs, dict) and all(content.get(docs.get(name, "")) == data for name, data in json_parts.items())
        if not any(covers(revision) for revision in revisions):
            raise ExportError("historical source content not covered")
    return manifest


def export(path: Path, storage: cutover.ReleaseStorage, owner: str) -> str:
    marker_path = path / MARKER
    if marker_path.is_file():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("version") == 1 and marker.get("verified") is True and type(marker.get("asset_id")) is int:
            raw = state_backup.decrypt_backup(storage.download(marker["asset_id"]), storage.password)
            verify(raw)
            return "already_verified"
        raise ExportError("invalid owner export marker")
    active = cutover.activate(path, storage, owner)
    if not active:
        raise ExportError("new identity must be verified before export")
    legacy_id = active.get("legacy_asset_id")
    assets = state_backup._assets(storage.token, storage.repo, storage.release["id"])
    if type(legacy_id) is not int or not any(x.get("id") == legacy_id and str(x.get("name") or "").startswith("michiflin-legacy-identity-") for x in assets):
        raise ExportError("legacy snapshot unavailable")
    raw = state_backup.decrypt_backup(storage.download(legacy_id), storage.password)
    state_backup._verify_plaintext(raw)
    with tempfile.TemporaryDirectory(prefix="owner-history-export-") as tmp:
        legacy = Path(tmp) / "legacy"
        state_backup._install_restored(raw, legacy)
        old, new = relay.identity(legacy), relay.identity(path)
        if old["fingerprint"] == new["fingerprint"] or old["transports"] & new["transports"]:
            raise ExportError("legacy and current identity are not separate")
        versions = collect_historical_sources(storage, Path(tmp), old["fingerprint"])
        all_patterns: set[bytes] = set()
        for folder in [legacy, *versions]:
            all_patterns.update(_patterns_from_path(folder))
        base_parts, base_counts = _build_parts(legacy, owner, all_patterns)
        historical = [_build_parts(folder, owner, all_patterns) for folder in versions]
        existing_id = os.environ.get("OWNER_HISTORY_EXISTING_ASSET_ID", "").strip()
        if existing_id:
            if not existing_id.isdigit() or int(existing_id) <= 0:
                raise ExportError("invalid existing owner archive")
            asset_id = int(existing_id)
            assets = state_backup._assets(storage.token, storage.repo, storage.release["id"])
            matches = [item for item in assets if item.get("id") == asset_id and
                       str(item.get("name") or "").startswith(PREFIX) and item.get("state") == "uploaded"]
            if len(matches) != 1:
                raise ExportError("existing owner archive is unavailable")
            downloaded = storage.download(asset_id)
            recovered = state_backup.decrypt_backup(downloaded, storage.password)
            manifest = verify_sources_covered(recovered, [(base_parts, base_counts), *historical])
            counts = manifest["counts"]
        else:
            parts, counts = merge_historical_parts(base_parts, base_counts, historical)
            archive = pack(parts, counts)
            encrypted = cutover.seal_bytes(archive, storage.password)
            if len(encrypted) > state_backup.MAX_BACKUP_BYTES:
                raise ExportError("owner archive exceeds the remote limit")
            name = PREFIX + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(8) + ".bin"
            url = f"https://uploads.github.com/repos/{storage.repo}/releases/{storage.release['id']}/assets"
            response = requests.post(url, headers={**state_backup.headers(storage.token), "Content-Type": "application/octet-stream"},
                                     params={"name": name}, data=encrypted, timeout=180, allow_redirects=False)
            response.raise_for_status()
            item = response.json()
            asset_id = item.get("id") if isinstance(item, dict) else None
            if type(asset_id) is not int or asset_id <= 0 or item.get("name") != name or item.get("state") != "uploaded":
                raise ExportError("owner archive upload unverified")
            downloaded = storage.download(asset_id)
            if not secrets.compare_digest(hashlib.sha256(downloaded).digest(), hashlib.sha256(encrypted).digest()):
                raise ExportError("owner archive changed during upload")
            recovered = state_backup.decrypt_backup(downloaded, storage.password)
            verify(recovered, expected=parts, expected_counts=counts)
        cutover._write_json(marker_path, {"version": 1, "verified": True, "asset_id": asset_id,
                                          "sha256": hashlib.sha256(downloaded).hexdigest(), "counts": counts})
    return "verified"


def main() -> int:
    try:
        if os.getenv("OWNER_ONLY") != "1":
            raise ExportError("owner-only mode required")
        result = export(Path(os.environ["CONFIG_DIR"]), cutover.ReleaseStorage(), os.environ.get("OWNER_ADDRESS", ""))
        print("Encrypted owner history export:", result)
        return 0
    except Exception as exc:
        print("Owner history export stopped safely (" + type(exc).__name__ + "); all legacy backups retained.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
