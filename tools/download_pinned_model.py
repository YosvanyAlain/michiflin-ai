"""Bounded parallel HTTPS range download of a fixed, checksum-pinned model.

No credentials, alternate providers, mirrors or retry-on-rate-limit behavior.
Called as an isolated UID with an empty environment. Each byte range is fetched
once; any quota, redirect error, wrong range, timeout or digest mismatch fails.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import os
import re
import threading
import time
import urllib.request
from urllib.parse import urlsplit


def download(url, size, digest, target):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'huggingface.co' or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('unreviewed model download')
    target = Path(target)
    stop = threading.Event()
    workers = 4
    width = (size + workers - 1) // workers
    parts = []
    def fetch(index):
        first, last = index * width, min(size, (index + 1) * width) - 1
        path = target.with_name(target.name + '.' + str(index))
        deadline = time.monotonic() + 1100
        request = urllib.request.Request(url, headers={'Range': f'bytes={first}-{last}', 'User-Agent': 'Michiflin-model-validation/1'})
        try:
            with urllib.request.urlopen(request, timeout=45) as response, path.open('wb') as output:
                expected = f'bytes {first}-{last}/{size}'
                if response.status != 206 or response.headers.get('Content-Range') != expected:
                    raise ValueError('model server refused exact byte range')
                length = 0
                while chunk := response.read1(256 * 1024):
                    if stop.is_set() or time.monotonic() > deadline:
                        raise TimeoutError('model download deadline')
                    length += len(chunk)
                    if length > last - first + 1:
                        raise ValueError('oversized model range')
                    output.write(chunk)
                if length != last - first + 1:
                    raise ValueError('incomplete model range')
            return path
        except BaseException:
            stop.set()
            raise
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            parts = list(pool.map(fetch, range(workers)))
        check = hashlib.sha256()
        with target.open('wb') as output:
            for path in parts:
                with path.open('rb') as source:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                        check.update(chunk)
        if target.stat().st_size != size or check.hexdigest() != digest:
            raise ValueError('model digest mismatch')
    finally:
        for index in range(workers):
            target.with_name(target.name + '.' + str(index)).unlink(missing_ok=True)


if __name__ == '__main__':
    download(os.environ['MODEL_URL'], int(os.environ['MODEL_BYTES']), os.environ['MODEL_SHA'], 'model.partial')
