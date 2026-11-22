"""Rebuild public screenshots/tours from current UI, with visibly synthetic data.

Run from repo: python tools/record_current_tours.py
Requires test dependencies, Playwright Chromium and ffmpeg. No mailbox, bot,
provider key, login, or live network service is used. Frames come from the real
renderer with the existing offline test bridge. Clips are intentionally held
frames, not a recording of email delivery speed or a physical phone test.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from michiflin import app_package, miniapp  # noqa: E402
from tests.test_webxdc_browser import browser_fixture  # noqa: E402


def label_capture(path: Path, locale: str) -> None:
    """Place the synthetic-data label outside, never on top of UI controls."""
    with Image.open(path) as source:
        image = source.convert('RGB')
    labelled = Image.new('RGB', (image.width, image.height + 24), '#152e2c')
    labelled.paste(image, (0, 0))
    try:
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 12)
    except OSError:
        font = ImageFont.load_default()
    label = 'DEMO · datos simulados' if locale == 'es' else 'DEMO · synthetic data'
    ImageDraw.Draw(labelled).text((8, image.height + 4), label, fill='white', font=font)
    labelled.save(path)


def encode(frames: list[Path], target: Path, temporary: Path) -> None:
    playlist = temporary / 'frames.txt'
    playlist.write_text(''.join("file '" + str(path) + "'\nduration 2\n" for path in frames)
                        + "file '" + str(frames[-1]) + "'\n")
    common = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', str(playlist)]
    subprocess.run(common + ['-vf', 'fps=10,scale=390:-2', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(target.with_suffix('.mp4'))], check=True)
    subprocess.run(common + ['-filter_complex', 'fps=4,scale=280:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse', '-loop', '0', str(target.with_suffix('.gif'))], check=True)


def main() -> None:
    digest = hashlib.sha256()
    # Fingerprint the actual names and bytes that enter the current .xdc, not
    # historical gzip copies retained only for compatibility verification.
    for name, source in app_package.entries(miniapp.APP_FILES):
        digest.update(name.encode() + (miniapp.APP_DIR / source).read_bytes())
    manifest = {'renderer_source_sha256': digest.hexdigest(), 'source': 'current packaged webxdc assets',
                'bridge': 'tests.test_webxdc_browser.BRIDGE', 'synthetic': True,
                'physical_device_test': False, 'clips_are_held_frames': True,
                'caption_strip_px': 24, 'languages': {}}
    for locale in ['es', 'en']:
        target = ROOT / 'docs' / 'media' / locale
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='michiflin-tour-') as directory:
            tmp = Path(directory)
            with browser_fixture(locale, {'width':390, 'height':844}, touch=True) as (page, errors):
                page.evaluate("window.__fixtureAdmin=false;cache.admin=false;window.__modes.kill_switch=false;cache.modes.kill_switch.active=false;goTab('home')")
                frames = []
                def capture(name: str, view: str = '', params=None) -> Path:
                    if view:
                        page.evaluate('([view,params])=>go(view,params)', [view, params or {}])
                    page.wait_for_timeout(300)
                    assert page.locator('[data-ui-recovery]').count() == 0, name
                    path = target / (name + '.png')
                    page.screenshot(path=str(path))
                    label_capture(path, locale)
                    saved = tmp / (str(len(frames)) + '.png')
                    saved.write_bytes(path.read_bytes()); frames.append(saved)
                    return saved
                overview = []
                for name, view in [('home','home'), ('ai','ai'), ('web','web'), ('apps','apps'), ('downloads','downloads'), ('settings','settings')]:
                    overview.append(capture(name, view))
                # A real, synthetic unread result demonstrates the bell and
                # center without asking a provider or sending any email.
                page.evaluate("""label => {goTab('home'); notice(label, 'ok',
                  {category:'ai', target:{view:'chat',params:{space:'agent'}}});}""",
                              'Respuesta de ejemplo' if locale == 'es' else 'Example response')
                page.locator('#notifications-toggle .notification-count').wait_for(state='visible')
                overview.append(capture('notification-bell'))
                page.locator('#notifications-toggle').click()
                page.locator('.notification-center-content').wait_for(state='visible')
                overview.append(capture('notification-center'))
                page.evaluate("closeSheet()")
                page.locator('#sheet').wait_for(state='hidden')
                page.evaluate("store.set('notices', []); bus.emit('notices')")
                # Follow the same deliberate route as the user, instead of
                # invoking the guide function directly or opening it on startup.
                page.evaluate("goTab('home')")
                page.locator('#view .link-card').click()
                page.locator('#view .btn.primary.wide').click()
                page.locator('.quick-tour').wait_for(state='visible')
                steps = page.evaluate('INTRO.length')
                for i in range(steps):
                    overview.append(capture('tour-step-' + str(i + 1)))
                    page.locator('.quick-tour .btn.primary').click()
                page.locator('#sheet').wait_for(state='hidden')
                encode(overview, target / 'clip-tour', tmp)
                capture('agent', 'chat', {'space':'agent'})
                capture('images', 'images')
                capture('tools', 'tools')
                capture('translator', 'translate')
                capture('youtube', 'youtube')
                capture('news', 'news')
                capture('rates', 'rates')
                capture('weather', 'weather')
                page.evaluate("window.__searchResults=[{title:'Documentation example',url:'https://example.org/guide',snippet:'Synthetic result for this demonstration.'}]")
                web = [capture('web', 'web'), capture('web-results', 'results', {'q':'example guide','engine':'web'}), capture('web-reader','reader',{'url':'https://example.org/guide'})]
                encode(web, target / 'clip-web', tmp)
                page.evaluate("setJobs([{id:'demo',title:'DEMO - example.txt',kind:'file',status:'queued',size:2048,updated:now()}],false)")
                downloads = [capture('download-queued','downloads')]
                page.evaluate("setJobs([{id:'demo',title:'DEMO - example.txt',kind:'file',status:'done',size:2048,updated:now()+1}],false)")
                downloads.append(capture('download-completed','downloads'))
                encode(downloads, target / 'clip-download', tmp)
                join = [capture('file-kit','downloads',{'section':'kit'}), capture('join','downloads',{'section':'kit','openJoin':True})]
                encode(join, target / 'clip-join', tmp)
                page.evaluate("prefs.theme='dark';applyLook()")
                capture('home-dark','home')
                page.evaluate("prefs.theme='light';applyLook();cache.admin=true;window.__fixtureAdmin=true")
                capture('admin','admin')
                assert errors == [], errors
            with browser_fixture(locale, {'width':1440, 'height':900}) as (page, errors):
                page.evaluate("window.__fixtureAdmin=false;cache.admin=false;goTab('home')")
                desktop = target / 'desktop-home.png'
                page.screenshot(path=str(desktop))
                label_capture(desktop, locale)
                assert errors == []
        manifest['languages'][locale] = {'viewport': [390, 844], 'desktop': [1440, 900],
                                         'manual_tour_steps': steps,
                                         'clips': ['clip-tour', 'clip-web', 'clip-download', 'clip-join']}
    (ROOT / 'docs' / 'media' / 'capture-manifest.json').write_text(json.dumps(manifest,indent=2) + '\n')


if __name__ == '__main__':
    main()
