"""Coder: a coding agent on free models that writes, runs and fixes a small project in an isolated sandbox.

It is separate from the extractive Agent: it uses the AI models (the "code" role, or one model chosen by the user).
Commands run as nobody, without network, secrets or access to the bot's files, with CPU, memory and time limits.
"""
from __future__ import annotations

import ast
import base64
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import ai, config, progress, state as runtime_state
from .util import tr

MAX_STEPS = 9
MAX_FILES = 40
MAX_FILE = 120_000
MAX_TOTAL = 400_000
MAX_BINARY = 60_000
RUN_TIMEOUT = 25
RUNS_PER_STEP = 4
OUTPUT_MAX = 2500
TIME_BUDGET = 780  # seconds; the app operation is cut at 900
ANSWER_TOKENS = 5000
ROOT = Path(tempfile.gettempdir()) / "michiflin-code"
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".pytest_cache", ".mypy_cache", "venv", ".venv"}
BLOCK = re.compile(r"<<<\s*(FILE|RUN|DONE|DELETE)\b[ \t]*([^\n>]*?)\s*>>>[ \t]*\n?(.*?)\n?[ \t]*<<<\s*END\s*>>>", re.S | re.I)
FENCE = re.compile(r"^\s*```[\w.+-]*\n(.*?)\n```\s*$", re.S)
EMPTY_PROJECT_CLAIM = re.compile(
    r"\b(?:no\s+(?:se\s+)?(?:construy[oó]|cre[oó]|gener[oó]|escribi[oó])\s+(?:nada|ning[uú]n\s+archivo)"
    r"|nothing\s+(?:was\s+|has\s+been\s+)?(?:built|created|generated|written)"
    r"|no\s+files?\s+(?:were|was|have\s+been)\s+(?:built|created|generated|written))\b", re.I,
)

_busy = threading.Semaphore(1)
_isolation: bool | None = None


class CoderError(Exception):
    pass


# ---------------------------------------------------------------------------
# Sandbox.
# ---------------------------------------------------------------------------
def _tool_path() -> str:
    folders = [os.path.dirname(sys.executable)]
    for tool in ("node", "gcc", "g++", "javac"):
        binary = shutil.which(tool)
        if binary:
            folders.append(os.path.dirname(os.path.realpath(binary)))
    folders += ["/usr/local/bin", "/usr/bin", "/bin"]
    return ":".join(dict.fromkeys(folders))


def _wrap(command: str) -> list[str]:
    """Run one shell command as nobody in fresh network, PID and mount namespaces with resource limits."""
    return [
        "sudo", "-n", "timeout", "-k", "3", str(RUN_TIMEOUT),
        "unshare", "--net", "--pid", "--fork", "--kill-child", "--mount-proc", "--",
        "setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--",
        "env", "-i", f"PATH={_tool_path()}", "HOME=/tmp", "LANG=C.UTF-8",
        # Bound JVM worker threads and virtual reservations on large CI hosts without relaxing sandbox limits.
        "JAVA_TOOL_OPTIONS=-Xms16m -Xmx256m -Xss512k -XX:MaxMetaspaceSize=256m "
        "-XX:CompressedClassSpaceSize=128m -XX:ReservedCodeCacheSize=64m -XX:ActiveProcessorCount=2 -XX:+UseSerialGC",
        "PYTHONDONTWRITEBYTECODE=1", "PYTHONIOENCODING=utf-8",
        "prlimit", "--cpu=20", "--as=2147483648", "--fsize=20971520", "--nproc=256", "--",
        "bash", "-c", "umask 000; " + command,
    ]


def isolation() -> bool:
    """True when commands can run fully isolated; without it the agent only writes files."""
    global _isolation
    if _isolation is None:
        _isolation = False
        if config.CODE_RUN and all(shutil.which(tool) for tool in ("sudo", "unshare", "setpriv", "prlimit", "timeout")):
            try:
                probe = subprocess.run(_wrap("id -u"), capture_output=True, text=True, timeout=20, cwd=tempfile.gettempdir())
                _isolation = probe.returncode == 0 and probe.stdout.strip() == "65534"
            except (OSError, subprocess.SubprocessError):
                _isolation = False
    return _isolation


def _lock_private() -> None:
    """Keep the bot's own folders unreadable for the sandbox user."""
    for folder in {config.CONFIG_DIR, config.JOB_ROOT, config.TEMP_ROOT, Path.home()}:
        try:
            path = Path(folder)
            if path.is_dir() and path.stat().st_uid == os.getuid():
                path.chmod(0o700)
        except OSError:
            continue


def _remove(folder: Path) -> None:
    if not folder.exists():
        return
    if isolation():
        subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(folder)], capture_output=True, timeout=30)
    shutil.rmtree(folder, ignore_errors=True)


def _run(folder: Path, command: str) -> tuple[int, str]:
    try:
        done = subprocess.run(_wrap(command), cwd=folder, capture_output=True, timeout=RUN_TIMEOUT + 10)
        output = (done.stdout + done.stderr).decode("utf-8", "replace")
        code = done.returncode
    except subprocess.TimeoutExpired:
        output, code = "", 124
    if code in {124, 137}:
        output += f"\n[stopped: {RUN_TIMEOUT} s or CPU limit reached]"
    if len(output) > OUTPUT_MAX:
        output = output[:OUTPUT_MAX // 2] + "\n[…]\n" + output[-OUTPUT_MAX // 2:]
    return code, output.strip()


# ---------------------------------------------------------------------------
# Project files.
# ---------------------------------------------------------------------------
def safe_path(raw: str) -> str:
    path = raw.strip().strip("`'\"").replace("\\", "/") if raw else ""
    while path.startswith("./"):
        path = path[2:]
    if path.startswith("/"):
        return ""
    path = re.sub(r"/{2,}", "/", path)
    parts = [part for part in path.split("/") if part]
    if not parts or len(path) > 140 or any(part in {".", ".."} or part.startswith("~") for part in parts):
        return ""
    if not all(re.fullmatch(r"[\w.\- ]{1,80}", part) for part in parts):
        return ""
    return "/".join(parts)



def _has_symlink(path: Path) -> bool:
    """Check existing components before creating directories or touching a target."""
    return any(part.is_symlink() for part in (path, *path.parents))


@contextmanager
def _safe_parent(folder: Path, raw: str, create: bool = False):
    """Traverse project directories by descriptor, never following a symlink."""
    path = safe_path(raw)
    if not path or not hasattr(os, "O_NOFOLLOW") or _has_symlink(folder):
        raise OSError("unsafe path")
    parts = path.split("/")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(folder, flags)
    try:
        for name in parts[:-1]:
            if create:
                try:
                    os.mkdir(name, mode=0o777, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _safe_write_text(folder: Path, raw: str, content: str, limit: int) -> bool:
    return _safe_write_bytes(folder, raw, content[:limit].encode("utf-8"))


def _safe_write_bytes(folder: Path, raw: str, data: bytes) -> bool:
    try:
        with _safe_parent(folder, raw, create=True) as (parent, name):
            flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(name, flags, 0o666, dir_fd=parent)
            with os.fdopen(fd, "wb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return False
                os.ftruncate(handle.fileno(), 0)
                handle.write(data)
        return True
    except OSError:
        return False


def _safe_delete(folder: Path, raw: str) -> bool:
    try:
        with _safe_parent(folder, raw) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                return False
            os.unlink(name, dir_fd=parent)
        return True
    except OSError:
        return False

def _unfence(content: str) -> str:
    match = FENCE.match(content)
    if match:
        return match.group(1)
    lines = content.split("\n")
    if lines and re.fullmatch(r"\s*```[\w.+-]*\s*", lines[0]):
        lines = lines[1:]
    if lines and re.fullmatch(r"\s*```\s*", lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines)


def write_files(folder: Path, files: list[dict[str, Any]]) -> None:
    for item in files[:MAX_FILES]:
        path = safe_path(str(item.get("path", "")))
        if not path:
            continue
        if "b64" in item:
            try:
                data = base64.b64decode(str(item["b64"]), validate=True)[:MAX_BINARY]
            except (ValueError, TypeError):
                continue
            _safe_write_bytes(folder, path, data)
        else:
            _safe_write_text(folder, path, str(item.get("text", "")), MAX_FILE)
    _open_up(folder)


def _open_up(folder: Path) -> None:
    try:
        if not _has_symlink(folder):
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if os.fstat(fd).st_uid == os.getuid():
                    os.fchmod(fd, 0o777)
            finally:
                os.close(fd)
    except OSError:
        return
    for index, path in enumerate(folder.rglob("*")):
        if index >= 3000:
            break
        try:
            relative = path.relative_to(folder).as_posix()
            with _safe_parent(folder, relative) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    info = os.fstat(fd)
                    if info.st_uid == os.getuid() and (stat.S_ISDIR(info.st_mode) or
                                                        (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
                        os.fchmod(fd, 0o777 if stat.S_ISDIR(info.st_mode) else 0o666)
                finally:
                    os.close(fd)
        except OSError:
            continue


def read_files(folder: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    total = 0
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if any(part in SKIP_DIRS for part in relative.parts) or path.suffix == ".pyc":
            continue
        try:
            with _safe_parent(folder, relative.as_posix()) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
                        continue
                    with os.fdopen(fd, "rb", closefd=False) as handle:
                        data = handle.read(MAX_FILE + 1)
                    if len(data) > MAX_FILE:
                        continue
                finally:
                    os.close(fd)
        except OSError:
            continue
        if total + len(data) > MAX_TOTAL or len(found) >= MAX_FILES:
            break
        try:
            text = data.decode("utf-8")
            if len(data) > MAX_FILE or "\x00" in text:
                raise UnicodeDecodeError("utf-8", data, 0, 1, "binary")
            found.append({"path": relative.as_posix(), "text": text})
        except UnicodeDecodeError:
            if len(data) > MAX_BINARY:
                continue
            found.append({"path": relative.as_posix(), "b64": base64.b64encode(data).decode()})
        total += len(data)
    return found


def project_name(files: list[dict[str, Any]]) -> str:
    """Human name of a project: manifest name, else the page title, else empty."""
    texts = {item["path"]: item.get("text", "") for item in files}
    found = re.search(r'^\s*name\s*=\s*"([^"\n]{1,60})"', texts.get("manifest.toml", ""), re.M)
    if not found:
        found = re.search(r"<title>\s*([^<\n]{1,60}?)\s*</title>", texts.get("index.html", ""), re.I)
    return found.group(1).strip() if found else ""


def tree(files: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {item['path']} ({len(item.get('text', '')) or len(item.get('b64', '')) * 3 // 4} bytes)" for item in files) or "(empty)"


# ---------------------------------------------------------------------------
# Agent loop.
# ---------------------------------------------------------------------------
def parse(text: str) -> list[tuple[str, str, str]]:
    return [(kind.upper(), arg.strip(), body) for kind, arg, body in BLOCK.findall(text or "")]


def _versions() -> str:
    found = [f"Python {sys.version.split()[0]}"]
    for label, executable, args in (("Node", "node", ["--version"]), ("GCC", "gcc", ["--version"]),
                                    ("G++", "g++", ["--version"]), ("Java", "javac", ["-version"])):
        binary = shutil.which(executable)
        if not binary:
            continue
        try:
            result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=10)
            version = (result.stdout or result.stderr).splitlines()[0].strip()
            if version:
                found.append(f"{label} {version}")
        except (OSError, subprocess.SubprocessError):
            continue
    return ", ".join(found) + ", bash"


def system_prompt(locale: str, can_run: bool) -> str:
    language = "Spanish" if locale == "es" else "English"
    run_block = ("<<<RUN>>>\nshell commands, already inside the project folder: use relative paths, never cd elsewhere (25 s, no network)\n<<<END>>>\n" if can_run else "")
    verify = ("- Verify your work by compiling/running the project and testing its main behavior. Use python3 -m unittest/pytest, "
              "node --check or node file.js for JavaScript, gcc for C, g++ for C++, and javac/java for Java when available. "
              "The sandbox automatically syntax-checks Python and available JavaScript/C/C++/Java sources after each round. "
              "When a command fails, read the diagnostic, fix the cause, and run the relevant check again.\n"
              "- Finish with DONE only when the requested build works or clearly state any missing dependency/tool.\n"
              if can_run else "- Commands cannot run here, so re-read your code carefully before DONE and state that it was not compiled.\n")
    return (
        "You are Coder, a careful senior software engineer working as an autonomous coding agent.\n"
        f"Workspace: a project folder on Linux with {_versions()}. There is no internet: you cannot install packages "
        "(no pip or npm install), so use only the standard library and built-in modules. Web pages must be self-contained "
        "(inline CSS and JS, no CDNs).\n"
        "Reply ONLY with action blocks, nothing else:\n"
        "<<<FILE relative/path.ext>>>\ncomplete file content\n<<<END>>>\n"
        + run_block +
        "<<<DELETE relative/path.ext>>>\n<<<END>>>\n"
        f"<<<DONE>>>\nsummary for the user in {language}: what you built and how to use or run it (2 to 6 short lines)\n<<<END>>>\n"
        "Rules:\n"
        "- First decide what the user wants. If the message is a question or a chat (for example 'do you know how to make X?', "
        "'what is Y?', 'can you explain Z?') and does not clearly ask you to build or change something, do NOT write files or run "
        "anything: reply only with a DONE block that answers briefly and asks what they would like you to build.\n"
        "- Always write whole files; never use placeholders or '...'.\n"
        "- Web pages: plain HTML, CSS and JS that open from index.html; keep element ids in the HTML and the JS identical. "
        "An automatic web check reports ids and local links that do not match, so do not write fake-DOM tests.\n"
        "- Delta Chat mini apps (webxdc, .xdc): when the user asks for a mini app, put index.html at the project root, load "
        "<script src=\"webxdc.js\"></script> before your own scripts (the messenger provides that file: never write it) and "
        "use window.webxdc for shared state: selfAddr, selfName, sendUpdate({payload: data}, \"\") and "
        "setUpdateListener(function (update) {...}, 0), which also replays every earlier update. Mini apps have no internet. "
        "Add manifest.toml with one line: name = \"Short app name\". The app packages the project as an .xdc file, so never write "
        "zip files or packaging scripts.\n"
        "- Keep the project small and clear (at most 12 files); add a short README.md when there is more than one file.\n"
        f"- Write user-facing text, comments and the README in {language} unless the user asks otherwise.\n"
        + verify
    )


PLACEHOLDER = re.compile(r"^\W*(\(?\s*\d+\s+lines?\s+written\s*\)?|\.\.\.|…|unchanged|same as before|\[wrote [^\]]*\])\W*$", re.I)


def _compact(text: str) -> str:
    """Older turns keep a note of each action but not the file bodies (and no action syntax the model could copy)."""
    def note(match: re.Match) -> str:
        kind, arg, body = match.group(1).upper(), match.group(2).strip(), match.group(3)
        if kind == "FILE":
            return f"[wrote {arg}: {body.count(chr(10)) + 1} lines]"
        if kind == "RUN":
            return f"[ran: {body.strip()[:200]}]"
        return f"[{kind.lower()} {arg}]".strip()
    return BLOCK.sub(note, text)[:4000]


ASKING = re.compile(r"^\W*(sabes|sabrías|sabrias|conoces|qué es|que es|qué son|que son|cómo funciona|como funciona|explícame|explicame|"
                    r"por qué|por que|do you know|what is|what's|what are|how does|explain|why)\b", re.I)
ORDER = re.compile(r"\b(hazme|haz una?|créame|creame|crea una?|construye|prográmame|programame|genera una?|escribe una?|"
                   r"make me|build me|create me|write me)\b|^\W*(make|build|create|write|add|change|fix)\b", re.I)
POLITE = re.compile(r"^\W*(puedes|podrías|podrias|me haces|me creas|me programas|me puedes|can you|could you|would you|please|por favor)\b", re.I)
BUILD = re.compile(r"\b(hacer|crear|construir|programar|generar|escribir|desarrollar|añadir|agregar|cambiar|arreglar|corregir|"
                   r"make|build|create|write|generate|develop|add|change|fix)\b", re.I)


def looks_like_question(text: str) -> bool:
    """A question without a direct order ('do you know how to make a mini app?') is answered, not built."""
    text = text.strip()
    if len(text) > 400 or ORDER.search(text) or POLITE.search(text):
        return False
    return bool(ASKING.search(text)) or (text.endswith("?") and not BUILD.search(text))


def run(request: str, files: list[dict[str, Any]], locale: str, model_key: str = "") -> dict[str, Any]:
    if not _busy.acquire(timeout=5):
        raise CoderError(tr(locale, "Programador ya está trabajando en otro proyecto. Prueba en unos minutos.",
                            "Coder is already working on another project. Try again in a few minutes."))
    try:
        runtime_state.check_work_allowed()
        return _run_project(request, files, locale, model_key)
    finally:
        _busy.release()


def _ask(messages: list[dict[str, Any]], model_key: str, sticky: str) -> ai.AIResult:
    if model_key:
        return ai.complete_exact(messages, model_key, max_tokens=ANSWER_TOKENS, raw=True)
    if sticky:
        result = ai.complete_exact(messages, sticky, max_tokens=ANSWER_TOKENS, raw=True)
        if result.ok:
            return result
    return ai.complete(messages, "code", max_attempts=6, max_tokens=ANSWER_TOKENS, raw=True)


ID_DEFINED = re.compile(r"""\bid\s*=\s*["'`]([\w-]+)|\.id\s*=\s*["'`]([\w-]+)|setAttribute\(\s*["']id["']\s*,\s*["'`]([\w-]+)""")
ID_USED = re.compile(r"""getElementById\(\s*["'`]([\w-]+)["'`]\s*\)|querySelector(?:All)?\(\s*["'`]#([\w-]+)["'`]\s*\)""")
LOCAL_REF = re.compile(r"""<(?:script[^>]+src|link[^>]+href)\s*=\s*["']([^"':?#]+\.(?:m?js|css))["']""", re.I)


def syntax_check(folder: Path, files: list[dict[str, Any]], can_run: bool) -> list[str]:
    """Parse Python and use isolated compiler syntax/build checks when toolchains are available."""
    problems: list[str] = []
    paths: dict[str, list[str]] = {"js": [], "c": [], "cpp": [], "java": []}
    for item in files:
        path = safe_path(str(item.get("path", "")))
        text = item.get("text")
        if not path or not isinstance(text, str):
            continue
        lower = path.lower()
        if lower.endswith(".py"):
            try:
                ast.parse(text, filename=path)
            except SyntaxError as exc:
                problems.append(f"{path}:{exc.lineno or 1}:{exc.offset or 1}: Python syntax error: {exc.msg}")
        elif lower.endswith((".js", ".mjs", ".cjs")):
            paths["js"].append(path)
        elif lower.endswith(".c"):
            paths["c"].append(path)
        elif lower.endswith((".cc", ".cpp", ".cxx")):
            paths["cpp"].append(path)
        elif lower.endswith(".java"):
            paths["java"].append(path)
    if not can_run:
        return problems
    if paths["js"] and shutil.which("node"):
        args = " ".join(shlex.quote("./" + path) for path in paths["js"])
        code, output = _run(folder, f"for f in {args}; do node --check -- \"$f\" || exit $?; done")
        if code:
            problems.append(f"JavaScript syntax check failed: {(output or 'node --check returned non-zero')[:500]}")
    for key, executable, language, standard in (("c", "gcc", "c", "c11"), ("cpp", "g++", "c++", "c++17")):
        if paths[key] and shutil.which(executable):
            args = " ".join(shlex.quote("./" + path) for path in paths[key])
            code, output = _run(folder, f"{executable} -std={standard} -Wall -Wextra -fsyntax-only -x {language} {args}")
            if code:
                problems.append(f"{executable} syntax check failed: {(output or 'compiler returned non-zero')[:500]}")
    if paths["java"] and shutil.which("javac"):
        args = " ".join(shlex.quote("./" + path) for path in paths["java"])
        scratch = "/tmp/michiflin-javac-" + re.sub(r"[^A-Za-z0-9_-]", "", folder.name)
        command = (f"mkdir -p {shlex.quote(scratch)} && javac -proc:none -d {shlex.quote(scratch)} {args}; "
                   "result=$?; " + f"rm -rf -- {shlex.quote(scratch)}; " + "exit $result")
        code, output = _run(folder, command)
        if code:
            problems.append(f"javac compile failed: {(output or 'javac returned non-zero')[:500]}")
    return problems


def web_check(files: list[dict[str, Any]]) -> list[str]:
    """Catch the classic text-only mistakes of web projects: JS ids missing from the HTML and broken local links."""
    texts = {item["path"]: item["text"] for item in files if "text" in item}
    pages = {path: text for path, text in texts.items() if path.lower().endswith((".html", ".htm"))}
    if not pages:
        return []
    scripts = {path: text for path, text in texts.items() if path.endswith((".js", ".mjs"))
               and not re.search(r"(^|[/._-])(tests?|spec)([/._-]|$)", path.lower())}
    for text in pages.values():
        for index, inline in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S | re.I)):
            scripts[f"inline script {index + 1}"] = inline
    defined = {name for text in [*pages.values(), *scripts.values()] for match in ID_DEFINED.findall(text) for name in match if name}
    problems = []
    for path, text in scripts.items():
        for name in sorted({name for match in ID_USED.findall(text) for name in match if name} - defined):
            problems.append(f"{path} looks up #{name}, but no element has id=\"{name}\"")
    for path, text in pages.items():
        base = posixpath.dirname(path)
        for ref in LOCAL_REF.findall(text):
            target = posixpath.normpath(posixpath.join(base, ref.removeprefix("./")))
            if not ref.startswith(("http", "//")) and target not in texts and target != "webxdc.js":
                problems.append(f"{path} links {ref}, but that file does not exist")
    return problems


def _wrap_up(messages: list[dict[str, Any]], model_key: str, sticky: str) -> str:
    """Out of rounds: ask only for the closing summary of what exists now."""
    final = messages + [{"role": "user", "content": "No rounds left. Do not write or run anything. Reply only with "
                         "<<<DONE>>>\nshort summary for the user, in their language: what was built, how to use it and "
                         "anything left unfinished\n<<<END>>>"}]
    result = _ask(final, model_key, sticky)
    if not result.ok:
        return ""
    found = [(body or arg).strip() for kind, arg, body in parse(result.text) if kind == "DONE"]
    if found:
        return found[0][:3000]
    text = result.text.strip()
    return text[:3000] if text and "<<<" not in text and len(text) < 3000 else ""


def _sticky_key(result: ai.AIResult) -> str:
    for item in ai.providers():
        if item.name == result.provider:
            for model in item.models:
                if model.title() == result.model:
                    return model.key
    return ""


def _run_project(request: str, files: list[dict[str, Any]], locale: str, model_key: str) -> dict[str, Any]:
    started = time.monotonic()
    can_run = isolation()
    if can_run:
        _lock_private()
    ROOT.mkdir(parents=True, exist_ok=True)
    ROOT.chmod(0o711)
    folder = Path(tempfile.mkdtemp(prefix="p", dir=ROOT))
    folder.chmod(0o777)
    steps: list[dict[str, Any]] = []
    summary, used, sticky = "", "", ""
    try:
        write_files(folder, files)
        context = ""
        if files:
            shown = read_files(folder)
            budget = 30_000
            parts = []
            for item in shown:
                body = item.get("text", "[binary file]")
                if budget - len(body) < 0:
                    parts.append(f"<<<FILE {item['path']}>>> (too long to show; read it with a command) <<<END>>>")
                    continue
                budget -= len(body)
                parts.append(f"<<<FILE {item['path']}>>>\n{body}\n<<<END>>>")
            context = "Current project files:\n" + "\n".join(parts) + "\n\n"
        hint = ("\n\n(This looks like a question: answer it in a DONE block and build nothing unless the user clearly asked for it.)"
                if looks_like_question(request) and not files else "")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(locale, can_run)},
            {"role": "user", "content": context + "Message from the user: " + request + hint},
        ]
        for step in range(MAX_STEPS):
            runtime_state.check_work_allowed()
            if time.monotonic() - started > TIME_BUDGET:
                break
            progress.phase(f"💻 Programando · paso {step + 1}", f"💻 Coding · step {step + 1}")
            result = _ask(messages, model_key, sticky)
            runtime_state.check_work_allowed()
            if not result.ok:
                if step == 0:
                    raise CoderError(ai.describe_error(result.error, locale, result, model_key).replace("⚠️ IA: ", "").replace("⚠️ ", ""))
                break
            used = result.signature()
            sticky = sticky or _sticky_key(result)
            actions = parse(result.text)
            if not actions:
                if step == 0 and not files:
                    summary = result.text.strip()[:3000]
                    break
                messages += [{"role": "assistant", "content": result.text[:3000]},
                             {"role": "user", "content": "Use only the action blocks described above."}]
                continue
            results, done, failed = [], "", False
            for kind, arg, body in actions:
                runtime_state.check_work_allowed()
                if kind == "FILE":
                    path = safe_path(arg)
                    if not path:
                        results.append(f"! Skipped unsafe path: {arg}")
                        continue
                    content = _unfence(body)
                    if PLACEHOLDER.match(content.strip() or "..."):
                        results.append(f"! {path} was NOT written: send the complete file content, never a placeholder.")
                        continue
                    if not _safe_write_text(folder, path, content, MAX_FILE):
                        results.append(f"! Skipped unsafe write: {path}")
                        continue
                    steps.append({"kind": "write", "path": path, "lines": content.count("\n") + 1})
                elif kind == "DELETE":
                    path = safe_path(arg)
                    if path and _safe_delete(folder, path):
                        steps.append({"kind": "delete", "path": path})
                    else:
                        results.append(f"! Skipped unsafe or missing delete: {arg}")
                elif kind == "DONE":
                    done = (body or arg).strip()
            _open_up(folder)
            runs = [body.strip() for kind, _arg, body in actions if kind == "RUN" and body.strip()][:RUNS_PER_STEP] if can_run else []
            for command in runs:
                runtime_state.check_work_allowed()
                code, output = _run(folder, command)
                runtime_state.check_work_allowed()
                failed = failed or code != 0
                steps.append({"kind": "run", "cmd": command[:400], "code": code, "out": output[:1500]})
                results.append(f"$ {command}\nexit {code}\n{output}")
            current_files = read_files(folder)
            problems = web_check(current_files) if current_files else []
            syntax_problems = syntax_check(folder, current_files, can_run) if current_files else []
            for label, found in (("web check", problems), ("syntax check", syntax_problems)):
                if found:
                    failed = True
                    report = "\n".join(found[:12])
                    steps.append({"kind": "run", "cmd": label, "code": 1, "out": report[:1500]})
                    results.append(f"$ {label}\nexit 1\n{report}")
            for message in messages[2:]:
                if message["role"] == "assistant":
                    message["content"] = _compact(message["content"])
            messages.append({"role": "assistant", "content": result.text[:24000]})
            if done and not failed:
                summary = done[:3000]
                break
            if len(messages) > 14:
                messages = messages[:2] + messages[-10:]
            state = "Project files now (read one with: cat path):\n" + tree(read_files(folder))
            if results:
                messages.append({"role": "user", "content": "Results:\n" + "\n\n".join(results)[:7000] + "\n\n" + state +
                                 "\n\nFix any error, or finish with <<<DONE>>> if everything works."})
            else:
                messages.append({"role": "user", "content": state + ("\n\nNow run the code to check it, then finish with <<<DONE>>>."
                                                                     if can_run else "\n\nFinish with <<<DONE>>> when the project is complete.")})
        runtime_state.check_work_allowed()
        output = read_files(folder)
        if output and not summary and time.monotonic() - started < TIME_BUDGET + 60:
            summary = _wrap_up(messages, model_key, sticky)
        if not output and not summary:
            raise CoderError(tr(locale, "Los modelos no produjeron código. Reformula la petición o prueba otro modelo.",
                                "The models produced no code. Rephrase the request or try another model."))
        if not summary:
            summary = tr(locale, "Terminé sin confirmación final; revisa el registro de pasos.", "Stopped without a final confirmation; check the step log.")
        written = {step["path"] for step in steps if step["kind"] == "write"}
        if written.intersection(item["path"] for item in output) and EMPTY_PROJECT_CLAIM.search(summary):
            summary = tr(locale, "Se escribieron archivos del proyecto; revísalos antes de usarlos.",
                         "Project files were written; review them before use.")
        return {"files": output, "summary": summary, "steps": steps[-60:], "sig": used, "isolated": can_run,
                "seconds": round(time.monotonic() - started), "name": project_name(output),
                "answer": not any(step["kind"] in {"write", "delete"} for step in steps)}
    finally:
        _remove(folder)
