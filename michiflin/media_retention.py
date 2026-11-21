"""Local downloaded audio/video copies, never identity keys or original user files."""
from pathlib import Path

MEDIA_SUFFIXES = frozenset({'.mp4', '.m4v', '.mkv', '.webm', '.avi', '.mov', '.3gp', '.mpeg',
                            '.mpg', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.flac', '.weba'})


def is_downloaded_media(name: str, mime: str = '') -> bool:
    parts = Path(str(name)).suffixes
    return (any(part.lower() in MEDIA_SUFFIXES for part in parts)
            or str(mime).lower().startswith(('audio/', 'video/')))


def account_blob(path: Path, config_dir: Path) -> bool:
    """Constrain deletions to regular non-symlink attachment copies in this account."""
    try:
        root = (config_dir / 'accounts').resolve()
        relative = path.resolve().relative_to(root)
        return (not path.is_symlink() and path.is_file()
                and any(part.endswith('-blobs') for part in relative.parts[:-1])
                and all(not parent.is_symlink() for parent in path.parents if parent != root))
    except (OSError, ValueError):
        return False
