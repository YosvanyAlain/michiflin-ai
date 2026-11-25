"""Password-protected PDF remains exportable; no password guessing or bypass."""
from __future__ import annotations

import base64
import io

import pytest
from pypdf import PdfWriter

from tests.book_reader_harness import open_test_reader
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_password_pdf_is_preserved_and_exportable_without_decryption(engine):
    pdf = PdfWriter()
    pdf.add_blank_page(width=400, height=600)
    pdf.encrypt('synthetic-password')
    output = io.BytesIO()
    pdf.write(output)
    raw = output.getvalue()
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate("go('books',{tab:'saved'})")
        page.locator('.book-file-input').set_input_files({'name': 'protected.pdf', 'mimeType': 'application/pdf', 'buffer': raw})
        page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for(timeout=30000)
        open_test_reader(page)
        page.locator('.book-file-status').get_by_text('No se eludió ninguna protección', exact=False).wait_for(timeout=30000)
        assert page.locator('.book-pdf-canvas').count() == 0
        page.evaluate('''() => {window.exportSheet=async (blob,name)=>{
          const bytes=new Uint8Array(await blob.arrayBuffer());let s='';for(const b of bytes)s+=String.fromCharCode(b);
          window.__export={name,data:btoa(s)};
        }}''')
        page.get_by_role('button', name='Exportar archivo original', exact=True).click()
        page.wait_for_function('window.__export')
        assert base64.b64decode(page.evaluate('window.__export.data')) == raw
        assert errors == []
