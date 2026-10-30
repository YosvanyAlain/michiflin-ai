"""Visual and integration tests of the kit using synthetic data, not a live bot."""
import io
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from michiflin import config, filekit, filekit_rar_native, filekit_transfer
from tests.test_webxdc_browser import browser_fixture, screenshot_if_requested, sync_playwright


@unittest.skipUnless(sync_playwright, "playwright is not installed")
class FileKitUiTests(unittest.TestCase):
    def test_offline_tree_edit_undo_archive_and_join_in_both_languages(self):
        engines = ["chromium"] + (["webkit"] if os.environ.get("MICHIFLIN_WEBKIT_TESTS") == "1" else [])
        for language, engine in ((language, engine) for language in ("es", "en") for engine in engines):
            with self.subTest(language=language, engine=engine), browser_fixture(language, {"width": 390, "height": 844}, engine=engine) as (page, errors):
                page.locator('.tab[data-tab="downloads"]:visible').click()
                page.locator('[role=tab][data-key=kit]').click()
                page.evaluate("""()=>{window.syntheticOriginal=new File(['original'], 'uno.txt',{type:'text/plain'});
                  window.webxdc.importFiles=async()=>[window.syntheticOriginal, new File(['segundo'], 'dos.txt',{type:'text/plain'})] }""")
                page.locator('[data-kit-action=import]').click()
                page.wait_for_selector('[data-kit-path="/uno.txt"]')
                page.locator('[data-kit-open="/uno.txt"]').click()
                page.wait_for_selector('.kit-editor')
                self.assertEqual(page.locator('.kit-editor').input_value(), 'original')
                page.locator('.kit-editor').fill('cambio uno')
                page.locator('[data-kit-action=save-text]').click()
                page.wait_for_function("async()=>await (await FileKitStore.get('/uno.txt')).blob.text()==='cambio uno'")
                # The editor obtains a fresh version after saving, so repeated edits work.
                page.wait_for_function("document.querySelector('.kit-editor')?.value==='cambio uno'")
                page.wait_for_timeout(150)
                page.locator('.kit-editor').fill('cambio dos')
                page.locator('[data-kit-action=save-text]').click()
                page.wait_for_function("async()=>await (await FileKitStore.get('/uno.txt')).blob.text()==='cambio dos'")
                page.locator('[data-kit-action=undo]').click()
                page.wait_for_function("async()=>await (await FileKitStore.get('/uno.txt')).blob.text()==='cambio uno'")
                page.locator('[data-kit-action=redo]').click()
                page.wait_for_function("async()=>await (await FileKitStore.get('/uno.txt')).blob.text()==='cambio dos'")
                page.locator('[data-kit-path="/uno.txt"] input').check()
                page.locator('[data-kit-action=archive-create]').click()
                page.locator('#sheet input[type=password]').fill('clave-sintetica')
                page.locator('[data-kit-action=run-archive]').click()
                page.wait_for_selector('[data-kit-path="/Paquete.zip"]', timeout=30000)
                page.locator('[data-kit-path="/uno.txt"] input').uncheck()
                page.locator('[data-kit-path="/Paquete.zip"] input').check()
                page.locator('[data-kit-action=archive-extract]').click()
                page.locator('#sheet input[type=password]').fill('clave-sintetica')
                page.locator('[data-kit-action=run-archive]').click()
                page.wait_for_selector('[data-kit-path="/Paquete - archivos"]', timeout=30000)
                self.assertEqual(page.evaluate("async()=>await (await FileKitStore.get('/Paquete - archivos/uno.txt')).blob.text()"), 'cambio dos')
                # Original synthetic File object was never changed by the app.
                self.assertEqual(page.evaluate("async()=>await window.syntheticOriginal.text()"), 'original')
                screenshot_if_requested(page, f"kit-local-{engine}-{language}")
                page.locator('[data-testid=filekit] .kit-heading button').click()
                self.assertEqual(page.evaluate('current.view'), 'downloads')
                self.assertTrue(page.locator('[data-testid=filekit] details.kit-join').evaluate('(el)=>el.open'))
                self.assertEqual(page.locator('.tab[data-tab=downloads]:visible').get_attribute('class').split()[-1], 'on')
                self.assertEqual(errors, [])

    def test_select_all_bulk_rename_preview_and_long_names(self):
        engines = ["chromium"] + (["webkit"] if os.environ.get("MICHIFLIN_WEBKIT_TESTS") == "1" else [])
        for language, engine in ((language, engine) for language in ("es", "en") for engine in engines):
            with self.subTest(language=language, engine=engine), browser_fixture(language, {"width": 390, "height": 844}, engine=engine) as (page, errors):
                long_name = "nombre-" + "muy-largo-" * 15 + ".mp4"
                page.evaluate("""async longName => {
                  await FileKitStore.put('', [{path: longName, blob: new Blob(['uno'])},
                    {path:'otro.txt',blob:new Blob(['dos'])}, {path:'carpeta/interior.txt',blob:new Blob(['tres'])}]);
                }""", long_name)
                page.locator('.tab[data-tab="downloads"]:visible').click()
                page.locator('[role=tab][data-key=kit]').click()
                page.wait_for_selector('[data-kit-path="/otro.txt"]')
                tree = page.locator('.kit-tree')
                self.assertEqual(tree.get_attribute('role'), 'region')
                self.assertIn('carpeta', page.locator('[data-kit-path="/carpeta"] .kit-toggle').get_attribute('aria-label'))
                metrics = tree.evaluate("""el=>({full:el.scrollWidth, visible:el.clientWidth,
                  text:[...el.querySelectorAll('.kit-item-name')].map(n=>n.textContent),
                  trunc:getComputedStyle(el.querySelector('.kit-item-name')).textOverflow})""")
                self.assertGreater(metrics['full'], metrics['visible'])
                self.assertIn(long_name, metrics['text'])
                self.assertNotEqual(metrics['trunc'], 'ellipsis')
                tree.focus(); page.keyboard.press('ArrowRight')
                page.wait_for_function("document.querySelector('.kit-tree').scrollLeft > 0")
                master = page.locator('[data-kit-action=select-folder]')
                master.check()
                self.assertEqual(page.locator('.kit-row input:checked').count(), 3)  # root children; not hidden descendants
                page.locator('[data-kit-action=rename]').click()
                page.locator('[data-kit-bulk=value]').fill('prefijo-')
                self.assertIn('prefijo-otro.txt', page.locator('.kit-bulk-preview').inner_text())
                page.locator('[data-kit-bulk=submit]').click()
                page.wait_for_selector('[data-kit-path="/prefijo-otro.txt"]')
                paths = page.evaluate("async()=> (await FileKitStore.view()).entries.map(x=>x.path)")
                self.assertIn('/prefijo-'+long_name, paths)
                self.assertIn('/prefijo-carpeta/interior.txt', paths)
                page.locator('[data-kit-action=undo]').click()
                page.wait_for_selector('[data-kit-path="/otro.txt"]')
                self.assertIn('/'+long_name, page.evaluate("async()=> (await FileKitStore.view()).entries.map(x=>x.path)"))
                self.assertEqual(errors, [])

    def test_join_selection_from_kit_and_reject_missing_raw_parts(self):
        with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
            page.evaluate("""async()=>{await FileKitStore.put('',[
              {path:'video.bin.001',blob:new Blob(['uno'])},
              {path:'video.bin.003',blob:new Blob(['tres'])}])}""")
            page.locator('.tab[data-tab="downloads"]:visible').click()
            page.locator('[role=tab][data-key=kit]').click()
            page.wait_for_selector('[data-kit-path="/video.bin.003"]')
            page.locator('[data-kit-path="/video.bin.001"] input').check()
            page.locator('[data-kit-path="/video.bin.003"] input').check()
            page.locator('[data-kit-action=join-selected]').click()
            page.wait_for_function("document.querySelector('#view')?.textContent.includes('Faltan las partes 2')")
            self.assertTrue(page.get_by_role('button', name='Unir', exact=True).is_disabled())
            self.assertEqual(page.evaluate('current.view'), 'downloads')
            self.assertTrue(page.locator('[data-testid=filekit] details.kit-join').evaluate('(el)=>el.open'))
            page.evaluate("async()=>await FileKitStore.put('',[{path:'video.bin.002',blob:new Blob(['dos'])}])")
            page.locator('[data-kit-action=refresh]').click()
            page.wait_for_selector('[data-kit-path="/video.bin.002"]')
            for number in ('001','002','003'):
                page.locator(f'[data-kit-path="/video.bin.{number}"] input').check()
            page.locator('[data-kit-action=join-selected]').click()
            page.wait_for_function("document.querySelector('#view')?.textContent.includes('video.bin.003')")
            page.get_by_role('button', name='Unir', exact=True).click()
            page.get_by_role('button', name='Guardar copia en el kit').click()
            page.wait_for_function("async()=>{try{return await (await FileKitStore.get('/video.bin')).blob.text()==='unodostres'}catch(_){return false}}")
            self.assertEqual(errors, [])

    def test_remote_numbered_parts_join_stream_and_hash_are_private(self):
        engines = ["chromium"] + (["webkit"] if os.environ.get("MICHIFLIN_WEBKIT_TESTS") == "1" else [])
        for engine in engines:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory(prefix="filekit-ui-join-") as temporary, \
                 mock.patch.object(config, "JOB_ROOT", Path(temporary) / "jobs"), mock.patch.object(config, "MIN_FREE_DISK_BYTES", 0):
                space = filekit.Workspace("owner@example.org")
                space.put("", [{"path": "copia.rar.001", "source": io.BytesIO(b"uno")},
                               {"path": "copia.rar.002", "source": io.BytesIO(b"dos")},
                               {"path": "copia.rar.003", "source": io.BytesIO(b"tres")}])
                digest = hashlib.sha256(b"unodostres").hexdigest()
                with browser_fixture("es", {"width": 390, "height": 844}, real_filekit=True, engine=engine) as (page, errors):
                    page.locator('.tab[data-tab="downloads"]:visible').click()
                    page.locator('[role=tab][data-key=kit]').click()
                    page.locator('[data-testid=filekit] [role=tab][data-key=remote]').click()
                    page.wait_for_selector('[data-kit-path="/copia.rar.001"]')
                    page.locator('[data-kit-action=select-folder]').check()
                    page.locator('[data-kit-action=join-selected]').click()
                    page.locator('#sheet input[placeholder*="SHA-256"]').fill(digest)
                    page.locator('[data-kit-action=run-remote-join]').click()
                    page.wait_for_selector('[data-kit-path="/copia.rar"]', timeout=30000)
                    self.assertEqual(space.content('/copia.rar')[1].read_bytes(), b'unodostres')
                    self.assertEqual(space.content('/copia.rar.001')[1].read_bytes(), b'uno')
                    sent = str(page.evaluate("window.__sentUpdates.map(x=>x.payload).filter(p=>p?.op==='filekit')"))
                    self.assertNotIn(digest, sent)
                    self.assertEqual(errors, [])

    @unittest.skipUnless(os.environ.get("MICHIFLIN_SANDBOX_TESTS") == "1" and filekit_rar_native.available(), "native RAR sandbox required")
    def test_remote_rar_selection_and_encrypted_bridge(self):
        engines = ["chromium"] + (["webkit"] if os.environ.get("MICHIFLIN_WEBKIT_TESTS") == "1" else [])
        for language, engine in ((language, engine) for language in ("es", "en") for engine in engines):
            with self.subTest(language=language, engine=engine), tempfile.TemporaryDirectory(prefix="filekit-ui-rar-") as temporary, \
                 mock.patch.object(config, "JOB_ROOT", Path(temporary) / "jobs"), mock.patch.object(config, "MIN_FREE_DISK_BYTES", 0):
                space = filekit.Workspace("owner@example.org")
                fixture = Path(__file__).parent / "fixtures/filekit/test_read_format_rar5_encrypted_filenames.rar"
                with fixture.open("rb") as source:
                    space.put("", [{"path": "seguro.rar", "source": source}])
                with browser_fixture(language, {"width": 390, "height": 844}, real_filekit=True, engine=engine) as (page, errors):
                    page.locator('.tab[data-tab="downloads"]:visible').click()
                    page.locator('[role=tab][data-key=kit]').click()
                    page.locator('[data-testid=filekit] [role=tab][data-key=remote]').click()
                    page.wait_for_selector('[data-kit-path="/seguro.rar"]')
                    page.locator('[data-kit-path="/seguro.rar"] input').check()
                    page.locator('[data-kit-action=archive-extract]').click()
                    page.locator('#sheet input[type=password]').fill('password')
                    page.locator('[data-kit-action=run-archive]').click()
                    page.wait_for_selector('[data-rar-member="b.txt"]', timeout=30000)
                    self.assertEqual(page.locator('.kit-rar-row').count(), 4)
                    self.assertTrue(page.locator('[data-kit-action=rar-extract-selected]').is_disabled())
                    page.locator('[data-rar-member="b.txt"]').check()
                    screenshot_if_requested(page, f"kit-rar-select-{engine}-{language}")
                    page.locator('[data-kit-action=rar-extract-selected]').click()
                    page.wait_for_selector('[data-kit-path="/seguro - archivos"]', timeout=30000)
                    self.assertEqual(space.content('/seguro - archivos/b.txt')[1].read_bytes(), b'This is from b.txt')
                    self.assertNotIn('/seguro - archivos/a.txt', [item['path'] for item in space.view()['entries']])
                    sent = str(page.evaluate("window.__sentUpdates.map(x=>x.payload).filter(p=>p?.op==='filekit')"))
                    self.assertNotIn('password', sent)
                    self.assertNotIn('b.txt', sent)
                    self.assertIn('ciphertext', sent)
                    self.assertEqual(errors, [])

    def test_remote_read_write_and_password_are_sealed_in_transit(self):
        engines = ["chromium"] + (["webkit"] if os.environ.get("MICHIFLIN_WEBKIT_TESTS") == "1" else [])
        for engine in engines:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory(prefix="filekit-ui-remote-") as temporary, \
                 mock.patch.object(config, "JOB_ROOT", Path(temporary) / "jobs"), mock.patch.object(config, "MIN_FREE_DISK_BYTES", 0):
                space = filekit.Workspace("owner@example.org")
                space.put("", [{"path": "remoto.txt", "source": io.BytesIO(b"private original"), "mime": "text/plain"}])
                with browser_fixture("es", {"width": 390, "height": 844}, real_filekit=True, engine=engine) as (page, errors):
                    page.locator('.tab[data-tab="downloads"]:visible').click()
                    page.locator('[role=tab][data-key=kit]').click()
                    page.locator('[data-testid=filekit] [role=tab][data-key=remote]').click()
                    page.wait_for_selector('[data-kit-path="/remoto.txt"]')
                    page.locator('[data-kit-open="/remoto.txt"]').click()
                    page.wait_for_selector('.kit-editor')
                    self.assertEqual(page.locator('.kit-editor').input_value(), 'private original')
                    page.locator('.kit-editor').fill('private replacement á')
                    page.locator('[data-kit-action=save-text]').click()
                    page.wait_for_function("document.querySelector('.kit-editor')?.value==='private replacement á'")
                    page.wait_for_timeout(250)
                    self.assertEqual(space.content('/remoto.txt')[1].read_bytes(), 'private replacement á'.encode())
                    page.evaluate("""()=>{
                      window.syntheticMobile=new File(['from phone'], 'telefono.txt',{type:'text/plain'});
                      window.webxdc.importFiles=async()=>[window.syntheticMobile];
                      window.webxdc.sendToChat=async draft=>{window.syntheticDraft=draft};
                    }""")
                    page.locator('[data-kit-action=import]').click()
                    page.wait_for_function("!!window.syntheticDraft")
                    caption = page.evaluate("window.syntheticDraft.text")
                    self.assertTrue(filekit_transfer.is_import_caption(caption))
                    attachment = Path(temporary) / "synthetic-attachment.txt"
                    attachment.write_bytes(b"from phone")
                    filekit_transfer.receive("owner@example.org", SimpleNamespace(file=str(attachment), file_mime="text/plain"), caption)
                    self.assertEqual(space.content('/telefono.txt')[1].read_bytes(), b"from phone")
                    self.assertEqual(attachment.read_bytes(), b"from phone")
                    self.assertEqual(page.evaluate("async()=>await window.syntheticMobile.text()"), "from phone")
                    page.locator('[data-kit-action=refresh]').click()
                    page.wait_for_selector('[data-kit-path="/telefono.txt"]')
                    if os.environ.get("MICHIFLIN_SANDBOX_TESTS") == "1":
                        page.locator('[data-kit-path="/remoto.txt"] input').check()
                        page.locator('[data-kit-action=archive-create]').click()
                        page.locator('#sheet input[type=password]').fill('clave-sintetica-supersecreta')
                        page.locator('[data-kit-action=run-archive]').click()
                        page.wait_for_selector('[data-kit-path="/Paquete.zip"]', timeout=30000)
                        self.assertTrue(space.content('/Paquete.zip')[1].is_file())
                    payloads = str(page.evaluate("window.__sentUpdates.map(x=>x.payload).filter(p=>p?.op==='filekit')"))
                    self.assertNotIn('private replacement', payloads)
                    self.assertNotIn(hashlib.sha256(b'from phone').hexdigest(), payloads)
                    self.assertNotIn('clave-sintetica-supersecreta', payloads)
                    self.assertIn('ciphertext', payloads)
                    screenshot_if_requested(page, f"kit-remoto-{engine}-es")
                    self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
