"""Exercise the real offline file store in a browser; all data is synthetic."""
import contextlib
import functools
import http.server
import os
import threading
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1] / "webxdc"


@contextlib.contextmanager
def page_fixture(engine="chromium"):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = getattr(pw, engine).launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            if not page.evaluate("typeof FileKitStore !== 'undefined'"):
                page.add_script_tag(path=str(ROOT / "filekit-store.js"))
            try:
                yield page
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@unittest.skipUnless(sync_playwright, "playwright is not installed")
class FileKitStoreTests(unittest.TestCase):
    def test_operations_are_atomic_and_originals_are_not_changed(self):
        engines = ["chromium"] + (["webkit"] if os.environ.get("MICHIFLIN_WEBKIT_TESTS") == "1" else [])
        for engine in engines:
            with self.subTest(engine=engine), page_fixture(engine) as page:
                result = page.evaluate("""async () => {
                  const s = FileKitStore;
                  const original = new File(['original'], 'origen.txt', {type:'text/plain'});
                  await s.mkdir('', 'carpeta');
                  await s.put('/carpeta', [
                    {path:'uno.txt', blob:original},
                    {path:'sub/dos.txt', blob:new Blob(['dos'], {type:'text/plain'})}
                  ]);
                  await s.transfer(['/carpeta'], '', true).then(() => { throw Error('copy should conflict'); }, e => { if (e.message !== 'inside_source') throw e; });
                  await s.mkdir('', 'destino');
                  await s.transfer(['/carpeta/sub'], '/destino', true);
                  await s.write('/destino/sub/dos.txt', new Blob(['editado'], {type:'text/plain'}));
                  const copied = await (await s.get('/destino/sub/dos.txt')).blob.text();
                  const shared = await (await s.get('/carpeta/sub/dos.txt')).blob.text();
                  await s.undo();
                  const undone = await (await s.get('/destino/sub/dos.txt')).blob.text();
                  await s.redo();
                  await s.rename('/destino/sub', 'renombrado');
                  await s.transfer(['/destino/renombrado'], '/carpeta', false);
                  await s.remove(['/carpeta/renombrado']);
                  const deleted = !(await s.view()).entries.some(e => e.path === '/carpeta/renombrado');
                  await s.undo();
                  const restored = await (await s.get('/carpeta/renombrado/dos.txt')).blob.text();
                  const state = await s.view();
                  return { copied, shared, undone, deleted, restored, original:await original.text(),
                    paths:state.entries.map(e=>e.path), canUndo:state.canUndo, canRedo:state.canRedo };
                }""")
                self.assertEqual(result["copied"], "editado")
                self.assertEqual(result["shared"], "dos")
                self.assertEqual(result["undone"], "dos")
                self.assertTrue(result["deleted"])
                self.assertEqual(result["restored"], "editado")
                self.assertEqual(result["original"], "original")
                self.assertIn("/carpeta/renombrado/dos.txt", result["paths"])
                self.assertTrue(result["canUndo"])
                self.assertTrue(result["canRedo"])

    def test_bad_paths_collisions_and_partial_import_fail_closed(self):
        with page_fixture() as page:
            result = page.evaluate("""async () => {
              const s = FileKitStore;
              await s.mkdir('', 'a');
              await s.put('/a', [{path:'ok.txt',blob:new Blob(['ok'])}]);
              const errors = [];
              for (const task of [
                () => s.mkdir('', '..'),
                () => s.put('', [{path:'../escape',blob:new Blob(['x'])}]),
                () => s.put('', [{path:'bad\\\\name',blob:new Blob(['x'])}]),
                () => s.transfer(['/a'], '/a', false),
                () => s.put('/a', [{path:'nuevo.txt',blob:new Blob(['x'])},{path:'ok.txt',blob:new Blob(['bad'])}]),
                () => s.remove(['/no-existe']),
              ]) {
                try { await task(); errors.push('unexpected-success'); } catch (e) { errors.push(e.message); }
              }
              const view = await s.view();
              return {errors,paths:view.entries.map(e=>e.path), content:await (await s.get('/a/ok.txt')).blob.text()};
            }""")
            self.assertEqual(result["errors"], ["invalid_name", "invalid_name", "invalid_name", "inside_source", "exists", "missing"])
            self.assertEqual(sorted(result["paths"]), ["/a", "/a/ok.txt"])
            self.assertEqual(result["content"], "ok")

    def test_chunked_bytes_and_interrupted_import_do_not_change_visible_files(self):
        engines = ["chromium"] + (["webkit"] if os.environ.get("MICHIFLIN_WEBKIT_TESTS") == "1" else [])
        for engine in engines:
            with self.subTest(engine=engine), page_fixture(engine) as page:
                result = page.evaluate("""async () => {
                  const s=FileKitStore, data=new Uint8Array(9*1024*1024+17);
                  for (let i=0;i<data.length;i++) data[i]=(i*37)%251;
                  await s.put('',[{path:'grande.bin',blob:new Blob([data])}]);
                  const received=new Uint8Array(await (await s.get('/grande.bin')).blob.arrayBuffer());
                  let same=received.length===data.length;
                  for(let i=0;same && i<data.length;i++) if(received[i]!==data[i]) same=false;
                  const real=Blob.prototype.arrayBuffer; let calls=0;
                  Blob.prototype.arrayBuffer=function(){
                    calls++;
                    if(calls===2) return Promise.reject(new Error('simulated interruption'));
                    return real.call(this);
                  };
                  let failed=false;
                  try { await s.put('',[{path:'incompleto.bin',blob:new Blob([data])}]); }
                  catch(e) { failed=e.message==='simulated interruption'; }
                  finally { Blob.prototype.arrayBuffer=real; }
                  const v=await s.view();
                  return {same,failed,paths:v.entries.map(x=>x.path)};
                }""")
                self.assertTrue(result["same"])
                self.assertTrue(result["failed"])
                self.assertEqual(result["paths"], ["/grande.bin"])

    def test_history_cleanup_does_not_remove_an_active_import(self):
        with page_fixture() as page:
            result = page.evaluate("""async () => {
              const s=FileKitStore, original=Blob.prototype.arrayBuffer;
              let release, waiting;
              const entered = new Promise(resolve => { waiting=resolve; });
              Blob.prototype.arrayBuffer=function() {
                const target=this;
                return new Promise((resolve,reject) => {
                  release=()=>original.call(target).then(resolve,reject);
                  waiting();
                });
              };
              try {
                const importing=s.put('', [{path:'en-curso.txt',blob:new Blob(['datos'])}]);
                await entered;
                await s.clearHistory();
                release();
                await importing;
              } finally { Blob.prototype.arrayBuffer=original; }
              return await (await s.get('/en-curso.txt')).blob.text();
            }""")
            self.assertEqual(result, "datos")

    def test_concurrent_mutations_and_reload(self):
        with page_fixture() as page:
            page.evaluate("""async () => {
              await Promise.all(Array.from({length:30}, (_,i) => FileKitStore.mkdir('', 'c'+i)));
              await FileKitStore.put('/c0', [{path:'x.txt', blob:new Blob(['persisted'])}]);
            }""")
            page.reload()
            if not page.evaluate("typeof FileKitStore !== 'undefined'"):
                page.add_script_tag(path=str(ROOT / "filekit-store.js"))
            result = page.evaluate("""async () => {
              const s=FileKitStore, v=await s.view();
              await s.remove(['/c0/x.txt']);
              await s.clearHistory();
              const after=await s.view();
              return {count:v.entries.length, text:(await (await s.get('/c0/x.txt').catch(()=>({blob:new Blob(['gone'])}))).blob.text()),
                      canUndo:after.canUndo, canRedo:after.canRedo};
            }""")
            self.assertEqual(result["count"], 31)
            self.assertEqual(result["text"], "gone")
            self.assertFalse(result["canUndo"])
            self.assertFalse(result["canRedo"])


if __name__ == "__main__":
    unittest.main()
