"""Bounded, deterministic extractive summaries for unusually long app answers.

This never calls a model or a service. It selects intact passages in their
original order, including headings, from across the answer. Material where
selection could break code, commands, tables or attribution is left unchanged
for the inline paginator instead. No generated text is written to disk.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .answer import DEPENDENT, terms

MAX_PASSAGE_UNITS = 480
MAX_PASSAGES = 400
SUMMARY_UNITS = 3600
MIN_SUMMARY_UNITS = 1400

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S|^\s*\*\*[^*\n]{2,75}\*\*\s*:?\s*$")
_BULLET = re.compile(r"^[ \t]*(?:[-*•]|\d{1,3}[.)])\s+\S")
_SENTENCE = re.compile(r"(?<=[.!?。！？])\s+(?=[A-ZÁÉÍÓÚÑ¿¡\"“(])")
_LIST_NUMBER = re.compile(r"^(?:(?:punto|point|paso|step)\s+)?\d+(?:\.\d+)*[.):]\s*", re.I)
_CAUTION = re.compile(r"\b(?:importante|advertencia|precauci[oó]n(?:es)?|riesgo|riesgos|contraindicaci[oó]n(?:es)?|"
                      r"excepci[oó]n(?:es)?|limitaci[oó]n(?:es)?|evita|evitar|resumen|conclusi[oó]n|"
                      r"important|warning|caution|risks?|contraindication|exception|limitations?|"
                      r"avoid|summary|conclusion)\b", re.I)
_CRITICAL = re.compile(r"\b(?:advertencia|precauci[oó]n(?:es)?|riesgo|riesgos|contraindicaci[oó]n(?:es)?|"
                       r"excepci[oó]n(?:es)?|limitaci[oó]n(?:es)?|warning|caution|risks?|contraindication|"
                       r"exception|limitations?)\b", re.I)
# Never break source attribution or executable / structured material by
# selecting excerpts. It remains available through the existing exact paginator.
_UNSAFE = re.compile(
    r"`|~~~|https?://|\bwww\.|\[[sS]?\d+\]|!?\[[^\]\n]{1,200}\]\([^\n)]{1,500}\)|<[a-z!][^>\n]{0,200}>|"
    r"^[ \t]*(?:\|[^\n]+\||\$\s+\S|[{}\[\]][ \t]*,?$|\{[ \t]*[\"]|"
    r"(?:print|console\.log)[ \t]*\(|[\"][^\"\n]+[\"]\s*:|"
    r"(?:curl|wget|sudo|pip|npm|git|python|SELECT|INSERT|UPDATE|DELETE|CREATE|def|class|function|"
    r"import|from|print|const|let|var)\s+\S)", re.I | re.M,
)


def units(text: str) -> int:
    """Count as JavaScript does, including non-BMP characters and surrogates."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


@dataclass(frozen=True)
class _Passage:
    body: str
    heading: str


def _passages(text: str) -> list[_Passage] | None:
    # Bound expensive pattern matching even for adversarial model output.
    # Highly marked-up material is not safe to summarize extractively anyway.
    if len(text) > 120_000 or text.count("[") > 128 or text.count("<") > 128 or "\x00" in text:
        return None
    if _UNSAFE.search(text):
        return None
    blocks = re.split(r"\n[ \t]*\n+", text.strip())
    found: list[_Passage] = []
    heading = ""
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        if lines and _HEADING.match(lines[0]):
            title = lines.pop(0).strip()
            if units(title) > 80:
                return None
            heading = title
            block = "\n".join(lines).strip()
            if not block:
                continue
        lines = block.splitlines()
        if len(lines) > 1 and all(_BULLET.match(line) for line in lines if line.strip()):
            parts = [line.strip() for line in lines if line.strip()]
        elif units(block) <= MAX_PASSAGE_UNITS:
            parts = [block]
        else:
            sentences = _SENTENCE.split(block)
            if len(sentences) < 2:
                return None
            parts = []
            current = ""
            for sentence in sentences:
                if units(sentence) > MAX_PASSAGE_UNITS:
                    return None
                candidate = (current + " " + sentence).strip()
                if current and units(candidate) > MAX_PASSAGE_UNITS:
                    if DEPENDENT.match(sentence):
                        return None  # do not detach a qualifying follow-up
                    parts.append(current)
                    current = sentence
                else:
                    current = candidate
            if current:
                parts.append(current)
        for part in parts:
            if units(part) > MAX_PASSAGE_UNITS:
                return None
            if part.strip():
                if found and found[-1].heading == heading and DEPENDENT.match(part):
                    context = found[-1].body + "\n\n" + part
                    if units(context) > MAX_PASSAGE_UNITS:
                        return None  # preserve dependent text with its antecedent
                    found[-1] = _Passage(context, heading)
                else:
                    found.append(_Passage(part, heading))
                if len(found) > MAX_PASSAGES:
                    return None
    return found if len(found) >= 6 else None


def extract(text: str, question: str = "", maximum: int = SUMMARY_UNITS) -> str | None:
    """Select substantial, diverse verbatim passages; return None if unsafe.

    Six regions of the response are represented before extra passages are
    selected. Topic overlap and importance/caution terms influence ranking, but
    this is a heuristic: the caller must label it extractive and allow the user
    to read the full generated answer. It cannot guarantee every key point.
    """
    passages = _passages(text)
    if not passages:
        return None
    wordsets = [set(terms(p.body)) for p in passages]
    frequencies = Counter(word for words in wordsets for word in words)
    query = set(terms(question[:2000]))
    scores: list[float] = []
    for index, passage in enumerate(passages):
        words = wordsets[index]
        centrality = sum(math.log1p(frequencies[word]) for word in words) / math.sqrt(len(words) + 1)
        relevance = len(words & query) / math.sqrt(len(query) or 1)
        priority = 3.0 if _CAUTION.search(passage.body) else 0.0
        scores.append(centrality + 2.0 * relevance + priority + (0.25 if index == 0 else 0))

    selected: set[int] = set()
    fingerprints: set[str] = set()
    used = 0

    def add(index: int) -> bool:
        nonlocal used
        passage = passages[index]
        # Conservative accounting includes the heading even when an adjacent
        # passage shares it; the final output will therefore never exceed max.
        cost = units(passage.body) + units(passage.heading) + 8
        fingerprint = _LIST_NUMBER.sub("", passage.body.strip()).casefold()
        if index in selected or fingerprint in fingerprints or cost > maximum - used:
            return False
        selected.add(index)
        fingerprints.add(fingerprint)
        used += cost
        return True

    count, regions = len(passages), min(6, len(passages))
    # Do not silently omit explicit warnings or limitations. If they are too
    # numerous to preserve alongside useful coverage, show exact parts instead.
    critical = [i for i, passage in enumerate(passages) if _CRITICAL.search(passage.body)]
    for index in critical:
        add(index)
    missing_critical = any(
        index not in selected and _LIST_NUMBER.sub("", passages[index].body.strip()).casefold() not in fingerprints
        for index in critical
    )
    if used > maximum // 2 or missing_critical:
        return None
    for region in range(regions):
        start, stop = count * region // regions, count * (region + 1) // regions
        if any(index in selected for index in range(start, stop)):
            continue
        # First choose the strongest passage in each region; if it does not fit,
        # try shorter alternatives instead of discarding the region.
        for index in sorted(range(start, stop), key=lambda i: (-scores[i], i)):
            if add(index):
                break
    if any(
        not any(index in selected for index in range(count * region // regions, count * (region + 1) // regions))
        for region in range(regions)
    ):
        return None
    for index in sorted(range(count), key=lambda i: (-scores[i], i)):
        add(index)

    if len(selected) < 4 or used < MIN_SUMMARY_UNITS or len(selected) == count:
        return None
    result: list[str] = []
    previous_heading = ""
    for index in sorted(selected):
        passage = passages[index]
        if passage.heading and passage.heading != previous_heading:
            result.append(passage.heading)
        previous_heading = passage.heading
        result.append(passage.body)
    rendered = "\n\n".join(result)
    return rendered if units(rendered) <= maximum else None
