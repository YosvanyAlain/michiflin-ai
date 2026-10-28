"""Model-free answers for the agent: Wikipedia, web search and extractive summaries ranked locally."""
from __future__ import annotations

import html
import math
import re
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

from . import tools
from .util import compact

STOP = set("""
a al algo algun alguna algunas alguno algunos ante antes aqui asi aun bajo bien cada como con contra cual cuales cuando de del desde
donde dos el ella ellas ello ellos en entre era eran es esa esas ese eso esos esta estaba estado estan estar este esto estos fue fueron
ha habia han hasta hay la las le les lo los mas me mi mis mucho muy nada ni no nos o otra otras otro otros para pero poco por porque
que quien quienes se sea ser si sido sin sobre son su sus tambien tan tanto te tiene tienen todo todos tu un una unas uno unos y ya
cual cuanto cuanta cuantos cuantas explica explicame dime decir sabes puedes quiero saber significa
the of and to in is are was were be been a an for on with as by at from that this these those it its or not but which who whom what
when where why how can could would should do does did has have had will about into than then there their they them you your me my
tell explain please know mean means
""".split())
SENTENCE = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÁÉÍÓÚÑ¿¡\"“(0-9])")
NOISE = re.compile(r"cookie|javascript|suscr[ií]bete|subscribe|iniciar sesi[oó]n|log in|sign up|newsletter|copyright|©|todos los derechos|"
                   r"all rights reserved|haz clic|click here|click (above|below|on|the)\b|leer m[aá]s|read more|compartir|share this|publicidad|advertisement|"
                   r"noindex|header:(on|off)|\b(top|bottom|left|right) (image|photo|picture)\b|\b(image|photo|picture) (above|below)\b|te presentamos|we present|^¡?(mira|descubre|aprende|conoce|lee|visita|consulta|sigue|visit|follow|read|explore|let’s|let's|ent[eé]rate|watch|learn|discover|find out|check out|see how)\b", re.I)
DEFINE = re.compile(r"(qu[eé] (es|son|era|fue)|qui[eé]n (es|era|fue)|what (is|are|was)|who (is|was))\b", re.I)
EXPLAIN = re.compile(r"(por ?qu[eé]|c[oó]mo|para qu[eé]|why|how)\b", re.I)
DATED = re.compile(r"^(?:[A-Z][a-z]{2} \d{1,2}, \d{4}|\d{1,2} [a-z]{3}\.? \d{4}|\d{4}-\d{2}-\d{2})\s*[·—-]\s*")
# Sentences that lean on the previous one or read like headings make poor stand-alone answers.
DEPENDENT = re.compile(r"^(y|and|esto|esta|este|estos|estas|eso|ello|as[ií]|por eso|por ello|sin embargo|adem[aá]s|tambi[eé]n|pero|entonces|"
                       r"por lo tanto|por tanto|this|these|that|those|it|they|he|she|however|also|so|thus|hence|therefore|consequently|but|moreover)\b", re.I)
QUESTION = re.compile(r"^(qu[eé]|qui[eé]n(es)?|c[oó]mo|cu[aá]ndo|d[oó]nde|por ?qu[eé]|cu[aá]l(es)?|cu[aá]nto[s]?|cu[aá]nta[s]?|"
                      r"what|who|how|when|where|why|which)\b\s*(es|son|fue|fueron|era|is|are|was|were|does|do|did)?\s*", re.I)


@dataclass
class Source:
    title: str
    url: str
    text: str
    weight: float = 1.0


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in text if not unicodedata.combining(char))


SUFFIXES = ("aciones", "iciones", "ciones", "cion", "mente", "ings", "ing", "ies", "ied", "es", "ed", "s")


def stem(word: str) -> str:
    """Light suffix stripping plus a six-letter prefix: a fair stem for Spanish and English."""
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            break
    return word[:6]


def terms(text: str) -> list[str]:
    """Lowercase, accent-free word stems without stop words."""
    return [stem(word) for word in re.findall(r"[a-z0-9]+", fold(text)) if len(word) > 2 and word not in STOP]


def topic(question: str) -> str:
    """The subject of a question, for encyclopedia lookups."""
    text = re.sub(r"^(expl[ií]ca(me)?|dime|cu[eé]ntame|define|explain|tell me)\s+", "", question.strip().lstrip("¿¡").rstrip("?!. "), flags=re.I)
    text = QUESTION.sub("", text)
    return re.sub(r"^(el|la|los|las|un|una|the|a|an)\s+", "", text, flags=re.I).strip() or question


def sentences(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for block in re.split(r"\n+", text):
        block = re.sub(r"\[\d+\]|\s+", " ", block).strip()
        if len(block) < 40 or block.count("|") > 2:
            continue
        for sentence in SENTENCE.split(block):
            sentence = sentence.strip(" -•·")
            if sentence.endswith("?") or sentence.startswith(("¿", "?")):
                continue  # headings that only repeat a question
            key = fold(sentence)
            if 40 <= len(sentence) <= 420 and key not in seen and not NOISE.search(sentence) and sentence.count("/") < 4:
                seen.add(key)
                found.append(sentence)
    return found


def _similar(first: set[str], second: set[str]) -> bool:
    """Near duplicates: high overlap, or one sentence saying little beyond the other."""
    if not first or not second:
        return False
    shared = len(first & second)
    return shared / len(first | second) > 0.55 or shared / min(len(first), len(second)) >= 0.8


def rank(question: str, sources: list[Source], limit: int = 4) -> list[tuple[str, int]]:
    """Pick the sentences that best cover the question (BM25-style weights), returned in reading order with their source index."""
    query = set(terms(question))
    numbers = set(re.findall(r"\b\d{3,4}\b", question))
    subject = terms(topic(question)) if DEFINE.match(question.strip(" ¿¡")) else []
    pool: list[tuple[int, int, str, list[str]]] = []
    for index, source in enumerate(sources):
        for position, sentence in enumerate(sentences(source.text)[:160]):
            pool.append((index, position, sentence, terms(sentence)))
    if not pool or not query:
        return []
    frequency = Counter(term for *_rest, words in pool for term in set(words))
    total = len(pool)
    average = sum(len(words) for *_rest, words in pool) / total or 1
    scored = []
    for index, position, sentence, words in pool:
        counts = Counter(words)
        hits = [term for term in query if term in counts]
        if not hits or (len(query) >= 3 and len(hits) < 2):
            continue
        score = 0.0
        for term in hits:
            idf = math.log(1 + (total - frequency[term] + 0.5) / (frequency[term] + 0.5))
            tf = counts[term]
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(words) / average))
        score *= sources[index].weight * (1.25 if position < 3 else 1.0) * (len(hits) / len(query)) ** 0.5
        if numbers and not numbers & set(re.findall(r"\b\d{3,4}\b", sentence)):
            score *= 0.35  # a question about 2022 is not answered by a sentence about 2026
        if DEPENDENT.match(sentence):
            score *= 0.55
        if subject and subject in (words[:len(subject)], words[1:len(subject) + 1]) and re.search(r"\b(es|son|is|are|fue|was)\b", sentence[:120]):
            score *= 1.8  # "A black hole is ..." answers "what is a black hole"
        if not re.search(r"[.!…»”\")]$", sentence) or (len(sentence) < 70 and ":" in sentence) or len(sentence.split()) < 6:
            score *= 0.45  # headings, labels and fragments
        scored.append((score, index, position, sentence, set(words)))
    scored.sort(key=lambda item: -item[0])
    chosen: list[tuple[float, int, int, str, set[str]]] = []
    for item in scored:
        if len(chosen) >= limit:
            break
        if item[0] < scored[0][0] * 0.35:
            break
        if any(_similar(item[4], other[4]) or fold(item[3])[:24] == fold(other[3])[:24] for other in chosen):
            continue
        chosen.append(item)
    # The best sentence leads; the rest keep reading order.
    chosen = chosen[:1] + sorted(chosen[1:], key=lambda item: (item[1], item[2]))
    return [(item[3], item[1]) for item in chosen]


def summarize(text: str, limit: int = 6) -> list[str]:
    """Extractive summary: sentences whose words recur most across the text, kept in their original order."""
    items = sentences(text)[:400]
    if len(items) <= limit:
        return items
    frequency = Counter(term for sentence in items for term in set(terms(sentence)))
    scored = []
    for position, sentence in enumerate(items):
        words = set(terms(sentence))
        if not words:
            continue
        score = sum(frequency[term] for term in words) / (len(words) ** 0.6)
        score *= 1.3 if position < 5 else 1.0
        scored.append((score, position, sentence, words))
    scored.sort(key=lambda item: -item[0])
    chosen: list[tuple[float, int, str, set[str]]] = []
    for item in scored:
        if len(chosen) >= limit:
            break
        if not any(_similar(item[3], other[3]) for other in chosen):
            chosen.append(item)
    return [item[2] for item in sorted(chosen, key=lambda item: item[1])]


def page_text(page) -> str:
    """Readable paragraphs of a loaded page: body text only, without menus, captions or citation marks."""
    paragraphs = []
    for block in getattr(page, "blocks", None) or []:
        if not re.match(r"\s*<(p|blockquote|dd)\b", block):
            continue
        text = html.unescape(re.sub(r"<sup\b.*?</sup>|<[^>]+>", " ", block, flags=re.S))
        text = re.sub(r"\s+([,.;:)])", r"\1", re.sub(r"\s+", " ", text)).replace("( ", "(").strip()
        if len(text) >= 40:
            paragraphs.append(text)
    if paragraphs:
        return "\n".join(paragraphs)
    return re.sub(r"(?<![.!?:])\n(?!\n)", " ", getattr(page, "text", "") or "")


def _page_text(url: str) -> str:
    from . import reader

    try:
        return page_text(reader.load(url))[:60000]
    except Exception:
        return ""


def gather(question: str, locale: str, results: list[tools.Result] | None = None, pages: int = 3) -> list[Source]:
    """Encyclopedia summary, search snippets and the text of the top pages, fetched in parallel within a time budget."""
    sources: list[Source] = []
    pool = ThreadPoolExecutor(max_workers=pages + 2)
    # Encyclopedia intros define things; they rarely explain why or how.
    explain = bool(EXPLAIN.match(question.strip(" ¿¡")))
    wiki = pool.submit(lambda: None if explain else tools.wiki_summary(topic(question), locale))
    found = pool.submit(lambda: results if results is not None else tools.search(question, locale, 8))
    try:
        hits = found.result(timeout=25) or []
    except Exception:
        hits = []
    try:
        from . import state
        state.check_work_allowed()
    except state.WorkPaused:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    readable = [hit for hit in hits if not re.search(r"youtube\.com|youtu\.be|facebook\.com|instagram\.com|tiktok\.com|x\.com/|twitter\.com", hit.url)]
    fetches = {pool.submit(_page_text, hit.url): hit for hit in readable[:pages]}
    done, _pending = wait(list(fetches) + [wiki], timeout=15)
    try:
        summary = wiki.result(timeout=0) if wiki in done else None
    except Exception:
        summary = None
    if summary and _about(summary[0], question):
        sources.append(Source(summary[0], summary[2], summary[1], 1.35))
    for future, hit in fetches.items():
        text = future.result() if future in done else ""
        if text:
            sources.append(Source(hit.title, hit.url, text, 1.0))
    snippets = "\n".join(DATED.sub("", hit.snippet) for hit in hits if hit.snippet)
    if snippets:
        sources.append(Source(tools_label(locale), "", snippets, 0.8))
    pool.shutdown(wait=False, cancel_futures=True)
    return sources


def _about(title: str, question: str) -> bool:
    """An encyclopedia article fits when most words of its title appear in the question."""
    words, asked = set(terms(title)), set(terms(question))
    return bool(words) and len(words & asked) / len(words) >= 0.75


def tools_label(locale: str) -> str:
    return "Resultados de búsqueda" if locale != "en" else "Search results"


def compose(question: str, sources: list[Source], locale: str, limit: int = 4) -> tuple[str, list[Source]]:
    """Answer text plus the sources it quotes, or an empty text when nothing relevant was found."""
    picked = rank(question, sources, limit)
    if not picked:
        return "", []
    cited: list[int] = []
    lines = []
    for sentence, index in picked:
        if sources[index].url and index not in cited:
            cited.append(index)
        mark = f" [{cited.index(index) + 1}]" if sources[index].url else ""
        lines.append(compact(sentence, 420) + mark)
    return " ".join(lines), [sources[index] for index in cited]
