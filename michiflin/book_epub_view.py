"""Bounded, inert reflowable EPUB presentation for the optional reader.

This is not a browser for book HTML. Only text, simple semantic blocks,
inline emphasis and locally embedded raster images survive. CSS, links,
JavaScript, SVG, forms and remote resources are never forwarded to the app.
"""
from __future__ import annotations

import base64
import io
import posixpath
import re
import warnings
from urllib.parse import unquote, urlsplit

from bs4 import Comment, NavigableString, Tag
from PIL import Image, UnidentifiedImageError

_MAX_BLOCKS = 5000
_MAX_IMAGES = 12
_MAX_IMAGE_PIXELS = 3_000_000
_MAX_IMAGE_SOURCE = 2_000_000
_MAX_IMAGE_BYTES = 110_000
_MAX_IMAGES_ENCODED = 500_000
_BOUNDARIES = {'p', 'div', 'section', 'article', 'blockquote', 'li', 'pre', 'figure', 'figcaption',
               'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
_KINDS = {'p', 'blockquote', 'li', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}


def _utf16len(value: str) -> int:
    # JavaScript positions count UTF-16 code units, not Python code points.
    return len(value.encode('utf-16-le', errors='surrogatepass')) // 2


def _image(archive, chapter: str, src: str, budget: dict) -> str | None:
    if budget['count'] >= _MAX_IMAGES or budget['bytes'] >= _MAX_IMAGES_ENCODED:
        budget['omitted'] += 1
        return None
    try:
        target = urlsplit(src)
        path = unquote(target.path)
        if (target.scheme or target.netloc or target.query or not path or path.startswith('/')
                or '\\' in path or '\x00' in path):
            raise ValueError('external image')
        name = posixpath.normpath(posixpath.join(posixpath.dirname(chapter), path))
        if name in {'.', '..'} or name.startswith('../'):
            raise ValueError('invalid image path')
        info = archive.getinfo(name)
        if info.file_size > _MAX_IMAGE_SOURCE or info.flag_bits & 1:
            raise ValueError('image too large or protected')
        data = archive.read(info)
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {'PNG', 'JPEG', 'GIF', 'WEBP'}:
                    raise ValueError('unsupported image')
                if image.width * image.height > _MAX_IMAGE_PIXELS or image.width < 1 or image.height < 1:
                    raise ValueError('image dimensions')
                image.seek(0)
                image.load()
                # Flatten alpha and metadata; no original bytes or active content.
                if image.mode in {'RGBA', 'LA'} or 'transparency' in image.info:
                    layer = image.convert('RGBA')
                    clean = Image.new('RGB', layer.size, 'white')
                    clean.paste(layer, mask=layer.getchannel('A'))
                else:
                    clean = image.convert('RGB')
                clean.thumbnail((1200, 1600))
                output = io.BytesIO()
                clean.save(output, format='JPEG', quality=72, optimize=True)
        raw = output.getvalue()
        encoded = base64.b64encode(raw).decode('ascii')
        if len(raw) > _MAX_IMAGE_BYTES or budget['bytes'] + len(encoded) > _MAX_IMAGES_ENCODED:
            raise ValueError('image budget')
        budget['count'] += 1
        budget['bytes'] += len(encoded)
        return 'data:image/jpeg;base64,' + encoded
    except (ValueError, KeyError, OSError, RuntimeError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        budget['omitted'] += 1
        return None


def chapter(archive, name: str, soup, budget: dict) -> list[dict]:
    """Extract all readable text without executing EPUB markup or fetching URLs."""
    result: list[dict] = []
    current = {'kind': 'p', 'chunks': [], 'units': 0, 'last': '', 'runs': []}
    visited = 0

    def flush() -> None:
        nonlocal current
        value = ''.join(current['chunks']).rstrip()
        if value:
            # Runs are measured in UTF-16 and are clipped again by the client.
            end = _utf16len(value)
            runs = [[start, min(stop, end), style] for start, stop, style in current['runs'] if start < end]
            result.append({'kind': current['kind'], 'text': value, 'runs': runs})
        current = {'kind': 'p', 'chunks': [], 'units': 0, 'last': '', 'runs': []}
        if len(result) > _MAX_BLOCKS:
            raise ValueError('too many blocks')

    def visit(node, kind: str = 'p', bold: bool = False, italic: bool = False, depth: int = 0) -> None:
        nonlocal visited
        visited += 1
        if visited > 150_000:
            raise ValueError('too many nodes')
        if depth > 160:
            raise ValueError('too deeply nested')
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            piece = str(node).replace('\r\n', '\n').replace('\r', '\n') if kind == 'pre' else re.sub(r'\s+', ' ', str(node))
            piece = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', piece)
            if kind != 'pre' and (not current['chunks'] or current['last'].isspace()):
                piece = piece.lstrip()
            if piece:
                current['kind'] = kind
                start = current['units']
                current['units'] += _utf16len(piece)
                if current['units'] > 3_000_000:
                    raise ValueError('text too large')
                current['chunks'].append(piece)
                current['last'] = piece[-1]
                if bold or italic:
                    current['runs'].append([start, current['units'], ('b' if bold else '') + ('i' if italic else '')])
                    if len(current['runs']) > 20_000:
                        raise ValueError('too many runs')
            return
        if not isinstance(node, Tag):
            return
        tag = node.name.lower() if node.name else ''
        if tag in {'head', 'script', 'style', 'iframe', 'object', 'embed', 'svg', 'input'}:
            return
        if tag == 'img':
            flush()
            data = _image(archive, name, str(node.get('src') or ''), budget)
            if data:
                result.append({'kind': 'image', 'data': data, 'alt': str(node.get('alt') or '')[:240]})
            return
        if tag == 'br':
            if current['chunks'] and current['last'] != '\n':
                current['chunks'].append('\n')
                current['units'] += 1
                current['last'] = '\n'
            return
        boundary = tag in _BOUNDARIES
        if boundary:
            flush()
        next_kind = kind
        if tag in _KINDS:
            next_kind = 'blockquote' if kind == 'blockquote' and tag == 'p' else tag
        for child in node.children:
            visit(child, next_kind, bold or tag in {'b', 'strong'}, italic or tag in {'em', 'i'}, depth + 1)
        if boundary:
            flush()

    visit(soup.body or soup)
    flush()
    return result


def compose(blocks: list[dict]) -> tuple[str, list[dict]]:
    """Return the full text and small offsets describing its presentation."""
    parts, view, offset = [], [], 0
    for block in blocks:
        kind = block['kind']
        if kind == 'image':
            view.append({'kind': 'image', 'at': offset, 'data': block['data'], 'alt': block['alt']})
            continue
        value = block['text']
        if parts:
            parts.append('\n\n')
            offset += 2
        start = offset
        offset += _utf16len(value)
        parts.append(value)
        item = {'kind': kind, 'start': start, 'end': offset}
        if block['runs']:
            item['runs'] = [[start + a, start + b, style] for a, b, style in block['runs']]
        view.append(item)
    return ''.join(parts), view
