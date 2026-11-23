"""Small, bounded relevance helpers shared by the public book catalogues.

These are heuristics, not proof of edition, completeness or download rights.
"""
from __future__ import annotations

import re
import unicodedata

_STOP = frozenset({
    'a', 'al', 'an', 'and', 'con', 'de', 'del', 'el', 'en', 'for', 'from', 'in',
    'la', 'las', 'los', 'of', 'or', 'para', 'por', 'the', 'to', 'un', 'una', 'y',
})
# These describe the kind of result rather than its subject. Keep them for
# ranking, but do not let a generic word like "tutorial" replace the topic.
_INTENT = frozenset({
    'aprende', 'aprender', 'basico', 'basics', 'book', 'books', 'curso',
    'descargar', 'download', 'ebook', 'ebooks', 'free', 'gratis', 'guia', 'guide',
    'intro', 'introduccion', 'introduction', 'learn', 'libro', 'libros', 'manual',
    'principiantes', 'programacion', 'programming', 'tutorial', 'tutorials',
})


def fold(value: object) -> str:
    return ''.join(char for char in unicodedata.normalize('NFKD', str(value or '').casefold())
                   if not unicodedata.combining(char))


def terms(query: str) -> list[str]:
    """A bounded sequence of safe search words; preserve short all-stopword titles."""
    words = list(dict.fromkeys(re.findall(r'[^\W_]+', fold(query), flags=re.UNICODE)))
    selected = [word for word in words if word not in _STOP]
    return (selected or words)[:12]


def primary(query: str) -> list[str]:
    words = terms(query)
    return [word for word in words if word not in _INTENT] or words


def language_bonus(value: object, locale: str) -> int:
    """Prefer the interface language when metadata supplies it; never exclude others."""
    languages = value if isinstance(value, (list, tuple)) else [value]
    normalized = {fold(item).strip().split('-')[0] for item in languages}
    preferred = {'es', 'spa', 'spanish', 'espanol'} if locale == 'es' else {'en', 'eng', 'english'}
    return 12 if normalized & preferred else 0


def contains(haystack: str, needle: str) -> bool:
    # Whole words prevent "Python" from matching "pythonism", "Java" from
    # matching "JavaScript", and very short terms from matching everywhere.
    return bool(needle and re.search(r'(?<!\w)' + re.escape(needle) + r'(?!\w)', haystack))


def same_edition(expected_title: str, actual_title: object, expected_authors: list, actual_authors: object) -> bool:
    """Conservative metadata match, not proof of rights or bibliographic identity."""
    def words(value: object) -> set[str]:
        if isinstance(value, (list, tuple)):
            value = ' '.join(str(part) for part in value[:80])
        return set(terms(str(value or '')))
    expected, actual = words(expected_title), words(actual_title)
    if not expected or not actual:
        return False
    shared = expected & actual
    if len(shared) < min(2, len(expected)) or len(shared) * 2 < len(expected):
        return False
    # A numbered title must not silently attach a different edition/volume.
    numbers = {word for word in expected if word.isdecimal()}
    if numbers and not numbers <= actual:
        return False
    left = {word for word in words(expected_authors) if len(word) >= 3}
    right = {word for word in words(actual_authors) if len(word) >= 3}
    return not left or not right or bool(left & right)


def relevance(query: str, title: object, authors: object = '', subjects: object = '', extra: object = '') -> int:
    """Rank title/author matches first and reject records without all key topics."""
    def field(value: object) -> str:
        if isinstance(value, (list, tuple)):
            return fold(' '.join(str(item) for item in value[:80]))
        return fold(value)
    title, authors, subjects, extra = (field(value) for value in (title, authors, subjects, extra))
    essential = primary(query)
    combined = ' '.join((title, authors, subjects, extra))
    if essential and not all(contains(combined, word) for word in essential):
        return -1
    words = terms(query)
    educational = {'tutorial', 'tutorials', 'guia', 'guide', 'curso', 'manual', 'learn', 'aprender', 'principiantes', 'introduction', 'introduccion'}
    teaching = ('tutorial', 'guide', 'guia', 'manual', 'curso', 'course', 'learn', 'aprender', 'programming',
                'programacion', 'computer language', 'beginner', 'principiante', 'introduction', 'introduccion', 'handbook', 'coding')
    fiction = ('screenplay', 'television', 'sketches', 'comedy', 'fiction', 'novel', 'monty python')
    if educational.intersection(words) and not any(contains(combined, marker) for marker in teaching):
        # Avoid comedy scripts or novels merely containing an ambiguous topic
        # such as "Python" when the person explicitly asked for a tutorial.
        # Keep an explicitly named comedy group, and never claim perfect NLP.
        if 'monty' not in essential and any(contains(combined, marker) for marker in fiction):
            return -1
    score = 6 if educational.intersection(words) and any(contains(combined, marker) for marker in teaching) else 0
    for word in words:
        score += (20 if contains(title, word) else 0) + (15 if contains(authors, word) else 0)
        score += (5 if contains(subjects, word) else 0) + (1 if contains(extra, word) else 0)
    return score
