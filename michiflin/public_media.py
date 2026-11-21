"""Small public image assets, fetched by the server rather than leaking browser requests."""
from __future__ import annotations

import base64
import io
import re
import threading
import time
from urllib.parse import urljoin, urlsplit

from PIL import Image, UnidentifiedImageError
from . import net

_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, str]] = {}
COVER_HOSTS = {'covers.openlibrary.org', 'books.google.com', 'books.googleusercontent.com', 'www.gutenberg.org',
               'gutenberg.org', 'gutenberg.pglaf.org', 'archive.org', 'standardebooks.org', 'www.standardebooks.org'}


def image_data(url: str, quality: str = 'low', *, cover: bool = False) -> str:
    def validate(value: str) -> None:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        allowed = COVER_HOSTS if cover else {'i.ytimg.com'}
        trusted_archive = cover and host.endswith('.archive.org')
        if (parsed.scheme != 'https' or (host not in allowed and not trusted_archive)
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError('image_source')
    validate(url)
    key = (url, quality)
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < 3600:
            return hit[1]
    current = url
    for _ in range(4):
        validate(current)
        response = net.request('GET', current, stream=True, follow_redirects=False, timeout=(8, 15))
        try:
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                if not cover or not response.headers.get('Location'):
                    raise ValueError('image_redirect')
                current = urljoin(current, response.headers['Location'])
                validate(current)  # only known public cover hosts, never arbitrary redirects
                continue
            raw = net.read_limited(response, 600_000)
            break
        finally:
            response.close()
    else:
        raise ValueError('image_redirect_limit')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 12_000_000:
                raise ValueError('image_dimensions')
            image.thumbnail({'low': (160, 120), 'medium': (320, 240), 'high': (640, 480)}.get(quality, (160, 120)))
            output = io.BytesIO()
            image.convert('RGB').save(output, 'JPEG', quality=68, optimize=True)
    except (UnidentifiedImageError, OSError):
        raise ValueError('image_format') from None
    if output.tell() > 90_000:
        raise ValueError('image_size')
    data = 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode()
    with _lock:
        _cache[key] = (time.monotonic(), data)
        while len(_cache) > 128:
            _cache.pop(next(iter(_cache)))
    return data


def youtube_thumbnails(ids: object, quality: str) -> dict:
    if not isinstance(ids, list) or not 1 <= len(ids) <= 6 or quality not in {'low', 'medium', 'high'}:
        raise ValueError('thumbnail_input')
    suffix = {'low': 'default.jpg', 'medium': 'mqdefault.jpg', 'high': 'hqdefault.jpg'}[quality]
    if any(not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', identifier) for identifier in ids):
        raise ValueError('video_id')
    result = []
    for identifier in dict.fromkeys(ids):
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', identifier):
            raise ValueError('video_id')
        try:
            data = image_data('https://i.ytimg.com/vi/' + identifier + '/' + suffix, quality)
            result.append({'id': identifier, 'data': data})
        except Exception:
            result.append({'id': identifier, 'data': ''})
    return {'images': result, 'quality': quality}
