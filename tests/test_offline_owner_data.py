"""Synthetic offline recovery of data kept in a prior WebXDC instance."""
from __future__ import annotations

import io
import zipfile

import pytest
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize("language", ["es", "en"])
def test_existing_local_project_and_chat_remain_viewable_offline(language):
    with browser_fixture(language, {"width": 375, "height": 800}) as (page, errors):
        page.evaluate("""() => {
          // Simulate an old instance whose bot no longer responds.
          window.webxdc.sendUpdate = () => {};
          saveFile = async (blob, name) => { window.__savedOffline = {name, bytes:[...new Uint8Array(await blob.arrayBuffer())]}; };
          store.set('coder', {name:'Synthetic project', summary:'kept locally', turns:[],
            files:[{path:'index.html',text:'<h1>Synthetic retained project</h1>'},{path:'readme.txt',text:'Synthetic notes'}]});
          store.set('chat.auto', [{role:'user',text:'Synthetic retained conversation',ts:1}]);
          go('coder');
        }""")
        assert page.get_by_text("index.html", exact=True).count() == 1
        page.get_by_role("button", name="Código fuente (ZIP)" if language == "es" else "Source code (ZIP)").click()
        page.get_by_role("button", name="Guardar o compartir" if language == "es" else "Save or share").click()
        page.wait_for_function("window.__savedOffline && window.__savedOffline.bytes.length > 0")
        saved = page.evaluate("window.__savedOffline")
        with zipfile.ZipFile(io.BytesIO(bytes(saved["bytes"]))) as archive:
            names = archive.namelist()
            html = next(name for name in names if name.endswith("index.html"))
            notes = next(name for name in names if name.endswith("readme.txt"))
            assert archive.read(html) == b"<h1>Synthetic retained project</h1>"
            assert archive.read(notes) == b"Synthetic notes"
        page.evaluate("go('chat', {space:'auto'})")
        assert page.get_by_text("Synthetic retained conversation").count() == 1
        assert errors == []
