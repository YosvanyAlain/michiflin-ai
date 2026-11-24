"""Synthetic EPUBs only: safe reflow, fallback, and old-client compatibility."""
from __future__ import annotations

import base64
import io
import zipfile
from unittest import mock

import pytest
from PIL import Image

from michiflin import book_legacy, book_library, miniapp


def fixture_epub(chapters, resources=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('META-INF/container.xml', '<container><rootfile full-path="OEBPS/book.opf"/></container>')
        manifest = ''.join(f'<item id="c{i}" href="c{i}.xhtml"/>' for i in range(len(chapters)))
        spine = ''.join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        archive.writestr('OEBPS/book.opf', f'<package xmlns="urn:opf"><manifest>{manifest}</manifest><spine>{spine}</spine></package>')
        for index, chapter in enumerate(chapters):
            archive.writestr(f'OEBPS/c{index}.xhtml', chapter)
        for name, data in (resources or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def read_fake(monkeypatch, chapters, resources=None, *, visual=True):
    raw = fixture_epub(chapters, resources)
    monkeypatch.setattr(book_library, '_fetch', lambda *args: (raw, 'application/epub+zip'))
    return book_library.reading_copy('https://example.org/book.epub', 'epub', visual=visual)


def from_utf16(value, start, end):
    return value.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')


def test_epub_opt_in_reflows_spine_with_inert_semantics_and_local_raster(monkeypatch):
    image = io.BytesIO()
    Image.new('RGBA', (5, 4), (255, 0, 0, 127)).save(image, 'PNG')
    chapters = [
        '<html><head><title>Hidden title</title></head><body><h1>Capítulo 🎉</h1>'
        '<p>Hola <strong>mundo</strong> y <em>lector</em>.</p>'
        '<img src="images/p.png" alt="Pintura" onerror="evil()">'
        '<img src="https://evil.example/steal.png"><img src="../../outside.png">'
        '<script>stealSecret()</script><style>badcss</style><iframe src="https://evil.example"></iframe>'
        '<p onclick="evil()">Final</p></body></html>',
        '<body><h2>Segunda parte</h2><p>Último capítulo.</p></body>',
    ]
    result = read_fake(monkeypatch, chapters, {'OEBPS/images/p.png': image.getvalue(), 'outside.png': image.getvalue()})
    assert result['complete'] and result['source_format'] == 'epub'
    assert result['text'] == 'Capítulo 🎉\n\nHola mundo y lector.\n\nFinal\n\nSegunda parte\n\nÚltimo capítulo.'
    assert result['presentation_warning'] == 'some_images_omitted'
    view = result['presentation']
    assert [block['kind'] for block in view] == ['h1', 'p', 'image', 'p', 'h2', 'p']
    heading, paragraph, illustration = view[:3]
    assert from_utf16(result['text'], heading['start'], heading['end']) == 'Capítulo 🎉'
    assert [from_utf16(result['text'], start, end) for start, end, _style in paragraph['runs']] == ['mundo', 'lector']
    assert [style for _start, _end, style in paragraph['runs']] == ['b', 'i']
    assert illustration['alt'] == 'Pintura' and illustration['data'].startswith('data:image/jpeg;base64,')
    with Image.open(io.BytesIO(base64.b64decode(illustration['data'].split(',')[1]))) as im:
        assert im.format == 'JPEG' and im.size == (5, 4)
    assert 'stealSecret' not in str(result) and 'evil.example' not in str(result) and 'onclick' not in str(result)


def test_default_legacy_response_is_unchanged_and_has_no_presentation(monkeypatch):
    chapter = '<html><body><p>One <em>two</em>.</p><script>bad()</script></body></html>'
    legacy = read_fake(monkeypatch, [chapter], visual=False)
    modern = read_fake(monkeypatch, [chapter], visual=True)
    assert 'presentation' not in legacy and legacy['text'] == 'One\ntwo\n.'
    assert modern['text'] == 'One two.' and 'presentation' in modern


def test_complex_epub_falls_back_to_complete_text_not_partial_markup(monkeypatch):
    content = '<body>' + ''.join(f'<p>Block {i}</p>' for i in range(5100)) + '</body>'
    result = read_fake(monkeypatch, [content])
    assert result['complete'] and result['presentation_warning'] == 'layout_unavailable'
    assert 'presentation' not in result and 'Block 0' in result['text'] and 'Block 5099' in result['text']


def test_excessive_or_invalid_image_never_causes_network_access(monkeypatch):
    image = io.BytesIO()
    Image.new('RGB', (3, 3), 'white').save(image, 'PNG')
    content = '<body><p>Complete.</p>' + '<img src="a.png">' * 14 + '<img src="data:image/svg+xml,unsafe"></body>'
    result = read_fake(monkeypatch, [content], {'OEBPS/a.png': image.getvalue()})
    assert result['text'] == 'Complete.' and result['complete']
    assert sum(row['kind'] == 'image' for row in result['presentation']) == 12
    assert result['presentation_warning'] == 'some_images_omitted'


def test_form_text_is_preserved_without_active_elements(monkeypatch):
    result = read_fake(monkeypatch, ['<body><form action="https://evil.example"><p>Readable inside.</p><input value="not text"></form></body>'])
    assert result['text'] == 'Readable inside.'
    assert 'evil.example' not in str(result) and 'not text' not in str(result)


def test_incomplete_or_protected_epub_still_rejected(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('META-INF/container.xml', '<container><rootfile full-path="OEBPS/book.opf"/></container>')
        archive.writestr('OEBPS/book.opf', '<package xmlns="urn:opf"><manifest><item id="a" href="missing.xhtml"/></manifest><spine><itemref idref="a"/></spine></package>')
    monkeypatch.setattr(book_library, '_fetch', lambda *args: (buffer.getvalue(), 'application/epub+zip'))
    with pytest.raises(book_library.BookError, match='epub_structure'):
        book_library.reading_copy('https://example.org/book.epub', 'epub', visual=True)


def test_opt_in_is_forwarded_without_changing_legacy_default(monkeypatch):
    fake = mock.Mock(return_value={'text': 'book', 'complete': True})
    monkeypatch.setattr(book_library, 'reading_copy', fake)
    scope = 'a' * 64
    miniapp.op_books(7, {'do': 'download', '_book_scope': scope, 'url': 'https://example.org/b.txt', 'format': 'txt'}, 'es')
    fake.assert_called_once_with('https://example.org/b.txt', 'txt')
    fake.reset_mock()
    miniapp.op_books(7, {'do': 'download', '_book_scope': scope, 'url': 'https://example.org/b.txt', 'format': 'txt', 'visual': True}, 'es')
    fake.assert_called_once_with('https://example.org/b.txt', 'txt', visual=True)


def test_validated_formats_only_and_no_auth_bypass_on_visual_fallback(monkeypatch):
    scope = 'b' * 64
    monkeypatch.setattr(book_legacy, '_alternatives', {})
    record = {'downloads': [{'url': 'https://example.org/book.epub', 'format': 'epub'},
                            {'url': 'https://example.org/book.txt', 'format': 'txt'}]}
    book_legacy._remember_formats(scope, record)
    fake = mock.Mock(side_effect=book_library.BookError('protected_book'))
    monkeypatch.setattr(book_library, 'reading_copy', fake)
    with pytest.raises(book_library.BookError, match='protected_book'):
        book_legacy.reading_copy(scope, 'https://example.org/book.epub', 'epub', visual=True)
    fake.assert_called_once_with('https://example.org/book.epub', 'epub', visual=True)


def test_preformatted_text_and_audio_fallback_are_inert_but_not_lost(monkeypatch):
    result = read_fake(monkeypatch, ['<body><pre>  one\n    two</pre><audio src="https://evil.example/s.mp3">Transcript text</audio></body>'])
    assert '  one\n    two' in result['text'] and 'Transcript text' in result['text']
    assert 'evil.example' not in str(result)
    assert result['presentation'][0]['kind'] == 'pre'
