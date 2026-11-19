"""The generated QR codes must actually scan back to the original text.

Drawing a plausible grid of squares is easy; drawing one a scanner can read
is the part worth testing. Every case here is rendered in a real browser and
then decoded again, so a mistake in the encoder shows up as an unreadable
code rather than as a pretty picture.

Two readers are used. zxing is the engine behind most phone cameras and is
the one that has to succeed, always. OpenCV's classic detector is far
stricter, so passing it as well buys margin for the conditions a real scan
meets -- an angle, glare, a cheap sensor. That extra check is only applied up
to version 15, because beyond it OpenCV cannot read even a flawless
reference code, which makes it a statement about OpenCV rather than about
this renderer.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QR = ROOT / "webxdc" / "qr.js"

LINK = ("https://i.delta.chat/#A1B2C3D4E5F60718293A4B5C6D7E8F9012345678"
        "&a=bot%40nine.testrun.org&n=Michiflin&i=xYz123&s=abcDEF")

STRICT_READER_CEILING = 15

_SCRIPT = (
    "const {qrEncode,qrSvg}=require(process.argv[1]);"
    "const o=JSON.parse(process.argv[3]);"
    "process.stdout.write(JSON.stringify({"
    "version:qrEncode(process.argv[2],o.level||'Q').version,"
    "svg:qrSvg(process.argv[2],o)}));"
)


def _missing() -> str:
    if shutil.which("node") is None:
        return "node is unavailable"
    for name in ("cv2", "zxingcpp", "playwright.sync_api"):
        try:
            __import__(name)
        except Exception:
            return f"{name.split('.')[0]} is unavailable"
    return ""


class QrCodeTests(unittest.TestCase):
    """Round-trip the renderer through a browser and back out of a decoder."""

    @classmethod
    def setUpClass(cls):
        reason = _missing()
        if reason:
            raise unittest.SkipTest(reason)
        from playwright.sync_api import sync_playwright
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.play = sync_playwright().start()
        cls.browser = cls.play.chromium.launch()
        cls.page = cls.browser.new_page(viewport={"width": 1400, "height": 1400})

    @classmethod
    def tearDownClass(cls):
        for close in (getattr(cls, "browser", None), getattr(cls, "play", None)):
            if close is not None:
                (close.close if hasattr(close, "close") else close.stop)()
        if getattr(cls, "_tmp", None) is not None:
            cls._tmp.cleanup()

    def _render(self, text: str, **options):
        """Paint the SVG in a browser and hand back the pixels and version."""
        import cv2
        done = subprocess.run(
            [shutil.which("node"), "-e", _SCRIPT, str(QR), text, json.dumps(options)],
            capture_output=True, text=True, timeout=120, check=True)
        payload = json.loads(done.stdout)
        page_file = self.tmp / "qr.html"
        shot = self.tmp / "qr.png"
        page_file.write_text(f"<body style='margin:0'>{payload['svg']}</body>", encoding="utf-8")
        self.page.goto(page_file.as_uri())
        self.page.locator("svg").screenshot(path=str(shot), scale="css")
        image = cv2.imread(str(shot))
        self.assertIsNotNone(image, "the rendered QR could not be read back")
        return image, payload["version"]

    def _assert_scans(self, text: str, **options):
        import cv2
        import zxingcpp
        image, version = self._render(text, **options)
        found = zxingcpp.read_barcode(image)
        self.assertTrue(
            found and found.text == text,
            f"a phone-grade reader failed on version {version}: "
            f"{found.text if found else None!r}")
        if version <= STRICT_READER_CEILING:
            strict, *_ = cv2.QRCodeDetector().detectAndDecode(image)
            self.assertEqual(strict, text,
                             f"the strict reader failed on version {version}")

    def test_every_error_level_scans_back_to_the_invitation_link(self):
        for level in ("L", "M", "Q", "H"):
            with self.subTest(level=level):
                self._assert_scans(LINK, level=level)

    def test_short_and_long_payloads_survive_the_round_trip(self):
        for text in ("hi", "OPENPGP4FPR:" + "A" * 40, "x" * 300, "y" * 900):
            with self.subTest(length=len(text)):
                self._assert_scans(text, level="Q")

    def test_unicode_payload_is_encoded_as_utf8(self):
        self._assert_scans("Michiflin . invitacion \u2728 \u4f60\u597d", level="Q")

    def test_the_shipped_styling_stays_scannable(self):
        """The defaults carry the gradient and rounding users actually see."""
        self._assert_scans(LINK)

    def test_rounder_eyes_than_shipped_would_lose_the_strict_reader(self):
        """Guards the tuning: the defaults sit just inside what is safe.

        If someone softens the eyes further for looks, this is the test that
        should start failing, rather than somebody's camera.
        """
        import cv2
        image, _ = self._render(LINK, level="Q", eyeRadius=18, pupilRadius=9)
        strict, *_ = cv2.QRCodeDetector().detectAndDecode(image)
        self.assertNotEqual(strict, LINK,
                            "heavier rounding unexpectedly passed; re-tune the defaults")

    def test_it_works_loaded_as_a_plain_script_the_way_the_miniapp_loads_it(self):
        """The shipped path: no bundler, no imports, just a global function.

        The other tests reach the renderer through Node, which would keep
        passing even if the file stopped working as a classic script.
        """
        import cv2
        import zxingcpp
        page_file = self.tmp / "browser.html"
        shot = self.tmp / "browser.png"
        page_file.write_text(
            '<!doctype html><meta charset="utf-8"><body style="margin:0">'
            '<div id="host"></div>'
            f'<script src="{QR.as_uri()}"></script>'
            '<script>document.getElementById("host").innerHTML ='
            f'  qrSvg({json.dumps(LINK)});</script></body>',
            encoding="utf-8")
        failures = []
        self.page.on("pageerror", lambda error: failures.append(str(error)))
        self.page.goto(page_file.as_uri())
        self.assertEqual(failures, [], "the script raised while loading")
        self.page.locator("svg").screenshot(path=str(shot), scale="css")
        image = cv2.imread(str(shot))
        found = zxingcpp.read_barcode(image)
        self.assertTrue(found and found.text == LINK, "in-page rendering did not scan")
        strict, *_ = cv2.QRCodeDetector().detectAndDecode(image)
        self.assertEqual(strict, LINK)

    def test_text_too_long_for_any_version_is_refused(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self._render("z" * 3000, level="H")


if __name__ == "__main__":
    unittest.main()
