"""Guards on what the long-running bot process pays for at import time.

The bot is meant to live on modest hardware for days at a time, so a heavy
dependency that only some jobs need must not be loaded at boot. These tests run
the import in a clean subprocess: once the suite is warm, every module is
already in sys.modules, so measuring inside the test process proves nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(code: str) -> str:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout.strip()


def test_importing_the_app_does_not_load_yt_dlp() -> None:
    """yt-dlp costs ~35 MiB and ~140 ms; downloads load it, booting must not."""
    loaded = _run("import sys; import michiflin.app; print('yt_dlp' in sys.modules)")
    assert loaded == "False", "michiflin.app pulled yt_dlp in at import time again"


def test_media_reports_availability_without_importing_yt_dlp() -> None:
    """available() is called per URL in web.py, so it must stay cheap."""
    out = _run(
        "import sys\n"
        "from michiflin import media\n"
        "print(media.available(), 'yt_dlp' in sys.modules)\n"
    )
    assert out == "True False", out


def test_media_attribute_access_still_resolves_the_real_module() -> None:
    """Lazy loading must not change what michiflin.media.yt_dlp means."""
    out = _run(
        "import sys\n"
        "from michiflin import media\n"
        "module = media.yt_dlp\n"
        "print(module is not None, module is sys.modules['yt_dlp'], media.gen_extractor_classes is not None)\n"
    )
    assert out == "True True True", out


def test_unknown_module_attribute_still_raises() -> None:
    """The lazy hook must not swallow genuine typos."""
    out = _run(
        "from michiflin import media\n"
        "try:\n"
        "    media.no_such_attribute\n"
        "except AttributeError as exc:\n"
        "    print('AttributeError', 'no_such_attribute' in str(exc))\n"
    )
    assert out == "AttributeError True", out
