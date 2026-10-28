"""Static checks for the mini app: translations are complete and every key the code uses exists."""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "webxdc"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONFIG_DIR", "/tmp/michiflin-test/config")
os.environ.setdefault("JOB_DIR", "/tmp/michiflin-test/jobs")
os.environ.setdefault("PREVIEW_DIR", "/tmp/michiflin-test/tmp")


def dictionaries() -> dict:
    script = f"{(APP / 'i18n.js').read_text()}\nprocess.stdout.write(JSON.stringify(I18N));"
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


@unittest.skipUnless(shutil.which("node"), "node is required")
class Scripts(unittest.TestCase):
    def test_no_global_function_is_defined_twice(self):
        """Scripts share one global scope, so a second definition silently replaces the first."""
        seen: dict[str, str] = {}
        for path in sorted((ROOT / "webxdc").glob("*.js")):
            for name in re.findall(r"^(?:async )?function (\w+)\(", path.read_text(), re.M):
                self.assertNotIn(name, seen, f"{name} in {path.name} and {seen.get(name)}")
                seen[name] = path.name

    def test_bridge_resends_acks_and_recovers_chunks(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "bridge.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("bridge ok", result.stdout)

    def test_all_app_scripts_parse(self):
        for path in sorted(APP.glob("*.js")):
            result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, f"{path.name}: {result.stderr[-2000:]}")

    def test_coder_history_diff_and_undo(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "coder_history.js")], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("coder history ok", result.stdout)

    def test_live_browser_startup_timeout_and_cleanup(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "live_web.js")], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("live web startup ok", result.stdout)


class Translations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.i18n = dictionaries()
        cls.code = "\n".join((APP / name).read_text() for name in ("app.js", "core.js", "coder.js", "live.js"))

    def test_same_keys(self):
        self.assertEqual(set(self.i18n["es"]) ^ set(self.i18n["en"]), set())

    def test_no_duplicate_translation_keys(self):
        source = (APP / "i18n.js").read_text()
        for language, end in (("es", "  en: {"), ("en", "\n};")):
            start = source.index(f"  {language}: {{")
            section = source[start:source.index(end, start)]
            keys = re.findall(r"^\s{4}([a-zA-Z_][a-zA-Z_0-9]*):", section, re.M)
            self.assertEqual(len(keys), len(set(keys)), language)

    def test_no_empty_values(self):
        for language, table in self.i18n.items():
            self.assertEqual([key for key, value in table.items() if not value.strip()], [], language)

    def test_placeholders_match(self):
        for key, text in self.i18n["es"].items():
            self.assertEqual(set(re.findall(r"\{(\w+)\}", text)), set(re.findall(r"\{(\w+)\}", self.i18n["en"][key])), key)

    def test_literal_keys_exist(self):
        used = set(re.findall(r'\bt\("([a-z0-9_]+)"\s*[,)]', self.code))
        used |= {key for key in re.findall(r'"((?:tile|topic|util|sugg|greet|intro)_[a-z0-9_]+)"', self.code)}
        self.assertEqual(sorted(used - set(self.i18n["en"])), [])

    def test_dynamic_keys_exist(self):
        from michiflin import ai, miniapp, queue

        tabs = re.findall(r'\{ id: "(\w+)", icon:', self.code)
        engines = json.loads(re.search(r"const ENGINES = (\[[^\]]+\])", self.code).group(1))
        helps = re.findall(r'\["(\w+)", "\w+"\]', re.search(r"const HELP = (\[.*?\]);", self.code).group(1))
        expected = {f"tab_{name}" for name in tabs} | {f"engine_{name}" for name in engines}
        expected |= {f"help_{name}_{part}" for name in helps for part in "tb"}
        expected |= {f"status_{name}" for name in queue.STATUS_LABELS} | {f"op_{name}" for name in miniapp.OPS}
        expected |= {f"role_{name}" for name in ai.ROLES}
        self.assertEqual(sorted(expected - set(self.i18n["en"])), [])

    def test_no_emoji_in_app(self):
        pictographic = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]")
        for path in APP.glob("*.*"):
            if path.suffix in {".js", ".css", ".html", ".toml"}:
                self.assertIsNone(pictographic.search(path.read_text()), path.name)


class Package(unittest.TestCase):
    def test_build_contains_every_file(self):
        import zipfile

        from michiflin import miniapp

        target = miniapp.build()
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(sorted(archive.namelist()), sorted(miniapp.APP_FILES))
        self.assertLess(target.stat().st_size, 400_000)



@unittest.skipUnless(shutil.which("node") and shutil.which("ffmpeg") and shutil.which("ffprobe"), "node and ffmpeg are required")
class SegmentJoin(unittest.TestCase):
    """Cut a video with the bot's own segmenter, rejoin it with the app's joiner and compare decoded frames."""

    def frames(self, path: Path, stream: str) -> list[str]:
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:{stream}", "-f", "framemd5", "-"], capture_output=True, text=True).stdout
        return [line.rsplit(",", 1)[-1].strip() for line in out.splitlines() if line and not line.startswith("#")]

    def join(self, target: Path, parts: list[Path]) -> dict:
        result = subprocess.run(["node", str(ROOT / "tests" / "mp4join.js"), str(target), *map(str, parts)], capture_output=True, text=True)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_segments_rejoin_to_the_same_video(self):
        import tempfile

        from michiflin import queue

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "Clip [360p].mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
                            "-t", "24", "-c:v", "libx264", "-preset", "veryfast", "-g", "50", "-bf", "2", "-c:a", "aac", "-shortest", str(source)], check=True)
            out = root / "parts"
            out.mkdir()
            parts = queue._segments(source, out, {"id": 9, "name": source.name, "duration": 24}, 300 * 1024)
            self.assertGreater(len(parts), 2)
            joined = root / "joined.mp4"
            report = self.join(joined, list(reversed(parts))[::-1])
            self.assertEqual([marker["index"] for marker in report["markers"]], list(range(1, len(parts) + 1)))
            self.assertEqual({marker["job"] for marker in report["markers"]}, {9})
            self.assertEqual({marker["name"] for marker in report["markers"]}, {parts[0].name.rsplit(" (", 1)[0] + ".mp4"})
            self.assertEqual(report["size"], joined.stat().st_size)
            self.assertEqual(subprocess.run(["ffmpeg", "-v", "error", "-i", str(joined), "-f", "null", "-"], capture_output=True, text=True).stderr, "")
            self.assertEqual(self.frames(joined, "v"), self.frames(source, "v"))
            self.assertLessEqual(abs(len(self.frames(joined, "a")) - len(self.frames(source, "a"))), 1)  # encoder priming only
            other = root / "other.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "2", "-c:v", "libx264", str(other)], check=True)
            self.assertEqual(self.join(root / "bad.mp4", [parts[0], other]).get("error"), "mismatch")


if __name__ == "__main__":
    unittest.main()
