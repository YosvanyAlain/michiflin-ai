"""Archive engine integration and adversarial checks; no real user files involved."""
import base64
import io
import os
import struct
import unittest
import zipfile
from pathlib import Path

from tests.test_filekit_store import page_fixture, sync_playwright

FIXTURES = Path(__file__).parent / "fixtures" / "filekit"
INIT = """() => {
  window.runKit = (job) => new Promise((resolve, reject) => {
    const worker = new Worker('/filekit-worker.js');
    const timer = setTimeout(() => { worker.terminate(); reject(Error('worker_timeout')); }, 30000);
    worker.onerror = (e) => { clearTimeout(timer); worker.terminate(); reject(Error(e.message)); };
    worker.onmessage = (e) => { clearTimeout(timer); worker.terminate(); resolve(e.data); };
    worker.postMessage(job);
  });
  window.from64 = (s) => Uint8Array.from(atob(s), c=>c.charCodeAt(0));
}"""


def b64(data):
    return base64.b64encode(data).decode("ascii")


@unittest.skipUnless(sync_playwright, "playwright is not installed")
class FileKitArchiveTests(unittest.TestCase):
    def test_zip_and_7z_password_volumes_and_empty_directories(self):
        engines = ["chromium"] + (["webkit"] if os.environ.get("MICHIFLIN_WEBKIT_TESTS") == "1" else [])
        for engine in engines:
            with self.subTest(engine=engine), page_fixture(engine) as page:
                page.evaluate(INIT)
                result = page.evaluate("""async () => {
                  const out={};
                  for(const format of ['zip','7z']) {
                    const made=await runKit({action:'create',format,name:'paquete.'+format,password:'clave-sintetica',
                      volumeBytes:128,limit:4*1024*1024,
                      files:[{path:'carpeta',type:'dir'},{path:'carpeta/vacia',type:'dir'},
                             {path:'carpeta/hola.txt',blob:new Blob(['contenido secreto áéíóú'],{type:'text/plain'})}]});
                    if(!made.ok){out[format]={stage:'create',code:made.code};continue;}
                    const volumes=made.result.files.map(f=>({path:f.path,type:f.type,blob:new Blob([f.bytes])}));
                    const first='paquete.'+format+'.001';
                    const common={files:volumes,first,limit:4*1024*1024};
                    const wrong=await runKit({...common,action:'extract',password:'incorrecta'});
                    const listing=await runKit({...common,action:'list',password:'clave-sintetica'});
                    const opened=await runKit({...common,action:'extract',password:'clave-sintetica'});
                    const txt=opened.result?.files.find(x=>x.path==='carpeta/hola.txt');
                    out[format]={made:made.ok,volumes:volumes.length,wrong:wrong.code,listed:listing.ok,
                      opened:opened.ok,code:opened.code,paths:opened.result?.files.map(x=>x.path),
                      text:txt ? new TextDecoder().decode(txt.bytes) : ''};
                  }
                  return out;
                }""")
                for fmt in ("zip", "7z"):
                    item = result[fmt]
                    self.assertTrue(item.get("made"), item)
                    self.assertGreaterEqual(item["volumes"], 2)
                    self.assertEqual(item["wrong"], "wrong_password")
                    self.assertTrue(item["listed"], item)
                    self.assertTrue(item["opened"], item)
                    self.assertIn("carpeta/vacia", item["paths"])
                    self.assertEqual(item["text"], "contenido secreto áéíóú")

    def test_encrypted_rar_and_rar_volumes(self):
        with page_fixture() as page:
            page.evaluate(INIT)
            names = ["test_read_format_rar_encryption_data.rar"] + [f"test_rar_multivolume_single_file.part{i}.rar" for i in (1, 2, 3)]
            payload = {name: b64((FIXTURES / name).read_bytes()) for name in names}
            result = page.evaluate("""async data => {
              const make=(names)=>names.map(path=>({path,blob:new Blob([from64(data[path])])}));
              const encrypted='test_read_format_rar_encryption_data.rar';
              const a=await runKit({action:'extract',files:make([encrypted]),first:encrypted,password:'12345678',limit:1024*1024});
              const wrong=await runKit({action:'extract',files:make([encrypted]),first:encrypted,password:'bad',limit:1024*1024});
              const multi=[1,2,3].map(i=>'test_rar_multivolume_single_file.part'+i+'.rar');
              const b=await runKit({action:'extract',files:make(multi),first:multi[0],password:'',limit:8*1024*1024});
              const missing=await runKit({action:'extract',files:make(multi.slice(0,1)),first:multi[0],password:'',limit:8*1024*1024});
              return {encrypted:a.ok,code:a.code,wrong:wrong.code,multivolume:b.ok,multiCode:b.code,
                      count:b.result?.files.length,missing:missing.ok};
            }""", payload)
            self.assertTrue(result["encrypted"], result)
            self.assertEqual(result["wrong"], "wrong_password")
            self.assertTrue(result["multivolume"], result)
            self.assertGreater(result["count"], 0)
            self.assertFalse(result["missing"])

    def test_empty_archive_and_tampered_declared_size_fail_safely(self):
        empty = io.BytesIO()
        with zipfile.ZipFile(empty, "w"):
            pass
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("large.txt", b"x" * 200_000)
        altered = bytearray(bomb.getvalue())
        central = altered.index(b"PK\x01\x02")
        struct.pack_into("<I", altered, central + 24, 1)  # lie about uncompressed size
        with page_fixture() as page:
            page.evaluate(INIT)
            result = page.evaluate("""async data => {
              const task=(raw,action,limit)=>runKit({action,files:[{path:'a.zip',blob:new Blob([from64(raw)])}],first:'a.zip',limit});
              return {empty:await task(data.empty,'extract',4096), broken:await task(data.broken,'extract',4096)};
            }""", {"empty": b64(empty.getvalue()), "broken": b64(altered)})
            self.assertTrue(result["empty"]["ok"], result)
            self.assertEqual(result["empty"]["result"]["files"], [])
            self.assertFalse(result["broken"]["ok"], result)

    def test_missing_listing_and_writes_outside_output_fail_closed(self):
        script = (Path(__file__).resolve().parents[1] / "webxdc/filekit-worker.js").read_text()
        script = script.replace('    const execute = (args) => {', '''
    if (job.first === "outside.zip") {
      const node = fs.lookupPath("/in/outside.zip").node;
      fileOps.stream.write({node, position:0}, new Uint8Array(1), 0, 1, 0, false);
    }
    if (job.first === "overflow.zip") {
      fs.writeFile("/out/dummy", new Uint8Array(0));
      const node = fs.lookupPath("/out/dummy").node;
      fileOps.stream.write({node, position:0}, new Uint8Array(limit + 1), 0, limit + 1, 0, false);
    }
    const execute = (args) => {''')
        script = script.replace('const listing = execute(["l", "-slt", "-bd", "-p" + password, archive]);',
                                'const listing = job.first === "listing.zip" ? "unexpected truncated listing" : execute(["l", "-slt", "-bd", "-p" + password, archive]);')
        with page_fixture() as page:
            page.context.route("**/filekit-worker.js", lambda route: route.fulfill(status=200, content_type="application/javascript", body=script))
            page.evaluate(INIT)
            result = page.evaluate("""async()=>{
              const out={};
              for(const name of ['outside','overflow','listing']) {
                const path=name+'.zip'; out[name]=await runKit({action:'extract',files:[{path,blob:new Blob(['x'])}],first:path,limit:4096});
              }
              return out;
            }""")
            self.assertEqual(result["outside"]["code"], "unsafe_path")
            self.assertEqual(result["overflow"]["code"], "output_limit")
            self.assertEqual(result["listing"]["code"], "unsafe_listing")

    def test_traversal_links_and_archive_bomb_are_rejected(self):
        archives = {}
        for name in ("traversal", "link", "bomb", "newline"):
            target = io.BytesIO()
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                if name == "traversal":
                    zf.writestr("../fuera.txt", "do not write outside")
                elif name == "link":
                    item = zipfile.ZipInfo("carpeta/enlace")
                    item.create_system = 3
                    item.external_attr = 0o120777 << 16
                    zf.writestr(item, "../../outside")
                elif name == "bomb":
                    zf.writestr("muy-grande.txt", b"x" * (2 * 1024 * 1024))
                else:
                    zf.writestr("linea\nSize = 1\nPath = otro", "x")
            archives[name] = b64(target.getvalue())
        with page_fixture() as page:
            page.evaluate(INIT)
            result = page.evaluate("""async data => {
              const out={};
              for(const [name,raw] of Object.entries(data)) {
                const path=name+'.zip';
                const r=await runKit({action:'extract',files:[{path,blob:new Blob([from64(raw)])}],first:path,
                                      password:'',limit:name==='bomb'?4096:1024*1024});
                out[name]={ok:r.ok,code:r.code};
              }
              return out;
            }""", archives)
            self.assertEqual(result["traversal"], {"ok": False, "code": "unsafe_path"})
            self.assertEqual(result["link"], {"ok": False, "code": "unsafe_link"})
            self.assertEqual(result["bomb"], {"ok": False, "code": "output_limit"})
            self.assertFalse(result["newline"]["ok"], result)


if __name__ == "__main__":
    unittest.main()
