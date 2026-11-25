"""Test-only access to dormant legacy readers, not shipped or exposed in the UI.

Library reading was deliberately removed from the product UI. These helpers
retain the existing security, storage and old-reader regression coverage by
exposing the pre-existing internal functions only in a synthetic page. Other
browser and packaged-app tests assert that no book-opening control is shipped.
"""
from __future__ import annotations

from michiflin import miniapp


_MARKER = '  return { mount, leave, stopSpeech, all, clearCache()'
_EXPOSE = '''  async function __testOpenBook(title) {
    const books = await allMeta();
    const matches = title ? books.filter(b => String(b.title || '').includes(title)) : books;
    if (matches.length !== 1) throw new Error('synthetic reader fixture needs one matching book');
    let book;
    try { book = await loadBook(matches[0]); } catch (_) { return missingOriginal(); }
    if (book.original_format && !(book.original instanceof Blob)) return missingOriginal();
    if (book.original instanceof Blob && ['epub', 'pdf'].includes(book.original_format)) {
      leave(); readerCleanup = BookOriginal.open(book); return;
    }
    reader(book);
  }
  return { __testOpenBook, mount, leave, stopSpeech, all, clearCache()'''


def open_test_reader(page, title: str = '') -> None:
    """Open exactly one named stored fixture using an instrumented page only."""
    if not page.evaluate('Boolean(window.BookShelf && window.BookShelf.__testOpenBook)'):
        source = (miniapp.APP_DIR / 'books.js').read_text(encoding='utf-8')
        assert source.count(_MARKER) == 1
        page.evaluate('source => (0, eval)(source)', source.replace(_MARKER, _EXPOSE))
    page.evaluate('title => window.BookShelf.__testOpenBook(title)', title)
