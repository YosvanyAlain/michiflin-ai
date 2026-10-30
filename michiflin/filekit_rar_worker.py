#!/usr/bin/env python3
"""Restricted child process: list RAR metadata or stream chosen files only.

This program runs only inside a networkless jail, with bounded memory, runtime,
input mounts and output files. It calls the separately licensed UnRAR library;
it does not create RAR archives. See vendor/unrar/LICENSE and NOTICE.txt.

UnRAR source code may be used in any software to handle RAR archives without
limitations free of charge, but cannot be used to develop RAR (WinRAR)
compatible archiver and to re-create RAR compression algorithm, which is
proprietary. Distribution of modified UnRAR source code in separate form or
as a part of other software is permitted, provided that full text of this
paragraph, starting from "UnRAR source code" words, is included in license, or
in documentation if license is not available, and in source code comments of
resulting package.
"""
from __future__ import annotations

import ctypes as ct
import json
import os
import re
import stat
import sys
from pathlib import Path

# The portable UnRAR ABI has one-byte structure packing on Unix too.
CALLBACK = ct.CFUNCTYPE(ct.c_int, ct.c_uint, ct.c_long, ct.c_long, ct.c_long)


class OpenData(ct.Structure):
    _pack_ = 1
    _fields_ = [
        ("ArcName", ct.c_char_p), ("ArcNameW", ct.c_wchar_p), ("OpenMode", ct.c_uint), ("OpenResult", ct.c_uint),
        ("CmtBuf", ct.c_void_p), ("CmtBufSize", ct.c_uint), ("CmtSize", ct.c_uint), ("CmtState", ct.c_uint),
        ("Flags", ct.c_uint), ("Callback", CALLBACK), ("UserData", ct.c_long), ("OpFlags", ct.c_uint),
        ("CmtBufW", ct.c_void_p), ("MarkOfTheWeb", ct.c_void_p), ("MaxDiskWrite", ct.c_uint),
        ("MaxAllocate", ct.c_uint), ("MaxRuntime", ct.c_uint), ("Reserved", ct.c_uint * 20),
    ]


class Header(ct.Structure):
    _pack_ = 1
    _fields_ = [
        ("ArcName", ct.c_char * 1024), ("ArcNameW", ct.c_wchar * 1024),
        ("FileName", ct.c_char * 1024), ("FileNameW", ct.c_wchar * 1024),
        ("Flags", ct.c_uint), ("PackSize", ct.c_uint), ("PackSizeHigh", ct.c_uint),
        ("UnpSize", ct.c_uint), ("UnpSizeHigh", ct.c_uint), ("HostOS", ct.c_uint),
        ("FileCRC", ct.c_uint), ("FileTime", ct.c_uint), ("UnpVer", ct.c_uint),
        ("Method", ct.c_uint), ("FileAttr", ct.c_uint), ("CmtBuf", ct.c_void_p),
        ("CmtBufSize", ct.c_uint), ("CmtSize", ct.c_uint), ("CmtState", ct.c_uint),
        ("DictSize", ct.c_uint), ("HashType", ct.c_uint), ("Hash", ct.c_char * 32),
        ("RedirType", ct.c_uint), ("RedirName", ct.c_void_p), ("RedirNameSize", ct.c_uint),
        ("DirTarget", ct.c_uint), ("MtimeLow", ct.c_uint), ("MtimeHigh", ct.c_uint),
        ("CtimeLow", ct.c_uint), ("CtimeHigh", ct.c_uint), ("AtimeLow", ct.c_uint), ("AtimeHigh", ct.c_uint),
        ("ArcNameEx", ct.c_void_p), ("ArcNameExSize", ct.c_uint),
        ("FileNameEx", ct.c_void_p), ("FileNameExSize", ct.c_uint), ("Reserved", ct.c_uint * 982),
    ]


class Failure(Exception):
    pass


def fail(code: str) -> None:
    raise Failure(code)


def valid_path(raw: str, directory: bool) -> str:
    if not isinstance(raw, str) or not raw or len(raw) >= 4096:
        fail("unsafe_path")
    value = raw.replace("\\", "/")
    if directory and value.endswith("/"):
        value = value[:-1]
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value) or len(value.encode("utf-8")) > 4096:
        fail("unsafe_path")
    parts = value.split("/")
    if len(parts) > 64:
        fail("unsafe_path")
    for part in parts:
        if (not part or part in {".", ".."} or re.search(r"[\x00-\x1f\x7f]", part)
                or len(part.encode("utf-8")) > 255):
            fail("unsafe_path")
    return value


def check_code(code: int, callback_error: str = "") -> None:
    if code == 0:
        return
    if callback_error:
        fail(callback_error)
    if code in (22, 24):
        fail("wrong_password")
    if code in (11, 25):
        fail("memory_unavailable")
    if code == 26:
        fail("archive_timeout")
    if code == 15:
        fail("missing_archive")
    fail("archive_failed")


def run(job: dict) -> dict:
    action = job.get("action")
    if action not in {"list", "extract"}:
        fail("invalid_request")
    password = job.get("password", "")
    if not isinstance(password, str) or len(password) > 1024 or any(c in password for c in "\x00\r\n"):
        fail("invalid_password")
    archive = job.get("archive", "")
    input_root = os.environ.get("MICHIFLIN_INPUT_ROOT", "/input")
    if not isinstance(archive, str) or Path(archive).parent != Path(input_root) or not Path(archive).is_file():
        fail("missing_archive")
    output_root = Path(os.environ.get("MICHIFLIN_OUTPUT_ROOT", "/work/result"))
    limit = job.get("outputLimit", 0)
    reserve = job.get("diskReserve", 0)
    memory_mib = job.get("memoryMiB", 512)
    if any(type(x) is not int for x in (limit, reserve, memory_mib)) or limit < 0 or reserve < 0 or memory_mib < 64:
        fail("invalid_request")
    selected = job.get("selected", [])
    if not isinstance(selected, list) or len(selected) > 1000 or any(not isinstance(x, str) for x in selected):
        fail("invalid_request")
    wanted = set(selected)
    if action == "extract" and (not wanted or len(wanted) != len(selected)):
        fail("nothing_selected")

    try:
        library = ct.CDLL(os.environ.get("MICHIFLIN_RAR_LIBRARY", "/runtime/libunrar.so"))
        library.RARGetDllVersion.restype = ct.c_int
        if library.RARGetDllVersion() != 11:
            fail("engine_unavailable")
        library.RAROpenArchiveEx.argtypes = [ct.POINTER(OpenData)]
        library.RAROpenArchiveEx.restype = ct.c_void_p
        library.RARReadHeaderEx.argtypes = [ct.c_void_p, ct.POINTER(Header)]
        library.RARReadHeaderEx.restype = ct.c_int
        library.RARProcessFile.argtypes = [ct.c_void_p, ct.c_int, ct.c_void_p, ct.c_void_p]
        library.RARProcessFile.restype = ct.c_int
        library.RARCloseArchive.argtypes = [ct.c_void_p]
        library.RARCloseArchive.restype = ct.c_int
    except (OSError, AttributeError):
        fail("engine_unavailable")

    current = {"fd": None, "written": 0, "expected": 0, "total": 0, "error": "", "checkAt": 0}

    @CALLBACK
    def callback(message: int, _user: int, p1: int, p2: int) -> int:
        try:
            if message == 4:  # UCM_NEEDPASSWORDW: never put the password in argv or the environment.
                if not password or p2 <= 0 or len(password) >= p2:
                    current["error"] = "wrong_password" if not password else "invalid_password"
                    return -1
                buffer = ct.create_unicode_buffer(password)
                ct.memmove(p1, buffer, ct.sizeof(buffer))
                return 1
            if message == 2:  # Narrow password fallback; UTF-8 conversion only when it fits.
                raw = password.encode("utf-8")
                if not raw or p2 <= 0 or len(raw) >= p2:
                    current["error"] = "wrong_password" if not raw else "invalid_password"
                    return -1
                ct.memmove(p1, raw + b"\0", len(raw) + 1)
                return 1
            if message in (0, 3):  # A missing volume is never requested interactively.
                if p2 == 0:
                    current["error"] = "missing_archive"
                    return -1
                return 1
            if message == 5:  # Refuse dictionaries larger than the library's safe default.
                current["error"] = "memory_unavailable"
                return -1
            if message == 1 and current["fd"] is not None:
                amount = int(p2)
                if amount < 0 or current["written"] + amount > current["expected"] or current["total"] + amount > limit:
                    current["error"] = "output_limit"
                    return -1
                if current["total"] >= current["checkAt"]:
                    fs = os.statvfs(output_root)
                    if fs.f_bavail * fs.f_frsize < reserve:
                        current["error"] = "disk_full"
                        return -1
                    current["checkAt"] = current["total"] + 4 * 1024 * 1024
                data = ct.string_at(p1, amount)
                offset = 0
                while offset < len(data):
                    count = os.write(current["fd"], data[offset:])
                    if count <= 0:
                        current["error"] = "disk_full"
                        return -1
                    offset += count
                current["written"] += amount
                current["total"] += amount
            return 1
        except Exception:
            current["error"] = "archive_failed"
            return -1

    info = OpenData()
    info.ArcNameW = archive
    info.OpenMode = 0 if action == "list" else 1
    info.Callback = callback
    info.MaxAllocate = memory_mib
    info.MaxRuntime = 900
    handle = library.RAROpenArchiveEx(ct.byref(info))
    if not handle:
        check_code(info.OpenResult or 13, current["error"])
    result = {"entries": [], "files": [], "solid": bool(info.Flags & 0x8), "multiVolume": bool(info.Flags & 0x1)}
    seen: set[str] = set()
    done: set[str] = set()
    try:
        if info.Flags & 0x1 and not info.Flags & 0x100:
            fail("missing_archive")
        while True:
            header = Header()
            name_buffer = ct.create_unicode_buffer(4097)
            header.FileNameEx = ct.addressof(name_buffer)
            header.FileNameExSize = len(name_buffer)
            code = library.RARReadHeaderEx(handle, ct.byref(header))
            if code == 10:
                break
            check_code(code, current["error"])
            if len(result["entries"]) >= 10000:
                fail("listing_limit")
            # FileNameEx is bounded. Do not silently accept a path that might
            # have been truncated at the end of the supplied native buffer.
            if len(name_buffer.value) >= len(name_buffer) - 1:
                fail("listing_limit")
            directory = bool(header.Flags & 0x20)
            path = valid_path(name_buffer.value, directory)
            if path in seen:
                fail("duplicate_path")
            seen.add(path)
            if header.RedirType or (header.HostOS == 3 and stat.S_ISLNK(header.FileAttr)):
                fail("unsafe_link")
            # Other Unix special files are not ordinary files and are not extracted.
            if header.HostOS == 3 and stat.S_IFMT(header.FileAttr) not in (0, stat.S_IFREG, stat.S_IFDIR):
                fail("unsafe_link")
            size = int(header.UnpSize) + (int(header.UnpSizeHigh) << 32)
            item = {"path": path, "type": "dir" if directory else "file", "size": size,
                    "encrypted": bool(header.Flags & 0x04)}
            result["entries"].append(item)
            chosen = action == "extract" and path in wanted
            if chosen and directory:
                fail("invalid_file")
            fd = None
            try:
                if chosen:
                    if size > limit - current["total"]:
                        fail("output_limit")
                    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                    filename = str(len(result["files"]))
                    fd = os.open(output_root / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    current.update(fd=fd, written=0, expected=size, error="")
                code = library.RARProcessFile(handle, 1 if chosen else 0, None, None)
                # RAR4 often reports encrypted data damage as CRC/data error
                # rather than a distinct bad-password code.
                if code == 12 and chosen and item["encrypted"] and password and not current["error"]:
                    fail("wrong_password_or_corrupt")
                check_code(code, current["error"])
                if chosen:
                    if current["written"] != size:
                        fail("archive_failed")
                    result["files"].append({"path": path, "slot": filename, "size": size})
                    done.add(path)
            finally:
                current["fd"] = None
                if fd is not None:
                    os.close(fd)
        if action == "extract" and done != wanted:
            fail("missing")
        return result
    finally:
        library.RARCloseArchive(handle)


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            fail("invalid_request")
        job = json.loads(raw)
        if not isinstance(job, dict):
            fail("invalid_request")
        result = {"ok": True, "result": run(job)}
    except Failure as exc:
        result = {"ok": False, "code": str(exc)}
    except BaseException:
        # Do not print an exception, paths, password, archive bytes or tracebacks.
        result = {"ok": False, "code": "archive_failed"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
