"""Mapping from the historical source list to the current WebXDC package.

The original gzip sources and notice remain in the repository so historical
package fingerprints can still be checked. The current package carries the
same complete source tar streams as smaller XZ archives, plus an accurate
notice. Neither archive is needed to run the offline 7-Zip engine.
"""
from __future__ import annotations

from collections.abc import Iterable

# Historical name -> (name in the .xdc, source on disk).
_PACKAGING_SUBSTITUTIONS = {
    "vendor/7zip/source-7zip-26.05.tar.gz": (
        "vendor/7zip/source-7zip-26.05.tar.xz", "vendor/7zip/source-7zip-26.05.tar.xz"),
    "vendor/7zip/source-wrapper-26.5.0.tar.gz": (
        "vendor/7zip/source-wrapper-26.5.0.tar.xz", "vendor/7zip/source-wrapper-26.5.0.tar.xz"),
    "vendor/7zip/NOTICE.txt": ("vendor/7zip/NOTICE.txt", "vendor/7zip/NOTICE-compact.txt"),
}


def entries(names: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """Return ordered (archive name, on-disk name) pairs; no data is omitted."""
    result = tuple(_PACKAGING_SUBSTITUTIONS.get(name, (name, name)) for name in names)
    if len({archive_name for archive_name, _ in result}) != len(result):
        raise ValueError("duplicate miniapp archive name")
    return result
