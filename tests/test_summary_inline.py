"""Meaningful, bounded summaries without extra model calls or lost answers."""
from __future__ import annotations

import time

import pytest

from michiflin import answer_inline, summary_inline
from tests.test_inline_answer import history, integration, raw_without_note  # noqa: F401


def long_prose(locale="es"):
    if locale == "es":
        sections = [
            ("La planificación", "planificación del espacio, la luz, la temperatura y las necesidades del cultivo"),
            ("El suelo", "suelo fértil, drenaje, materia orgánica y cuidado de las raíces"),
            ("El riego", "riego regular, humedad del suelo y ahorro de agua"),
            ("Las semillas", "semillas apropiadas, distancias entre plantas y tiempo de crecimiento"),
            ("El cuidado", "observación de plagas, ventilación y revisión de las hojas"),
            ("Las precauciones", "precauciones, herramientas seguras y protección de las plantas"),
        ]
        explain = ("Este aspecto merece una explicación concreta y suficiente para que la persona entienda "
                   "cuándo actuar, qué observar y cómo adaptar la decisión a las condiciones reales de su casa. ")
        extra = "No hace falta comprar materiales costosos si los recursos disponibles permiten un mantenimiento constante y seguro."
    else:
        sections = [
            ("Planning", "planning the space, sunlight, temperature and the needs of each crop"),
            ("The soil", "fertile soil, drainage, organic matter and healthy roots"),
            ("Watering", "regular watering, soil moisture and economical use of water"),
            ("The seeds", "appropriate seeds, spacing and the time needed for growth"),
            ("Care", "observing pests, ventilation and checking leaves"),
            ("Precautions", "precautions, safe tools and protecting the plants"),
        ]
        explain = ("This part needs a clear, useful explanation so the reader can decide when to act, "
                   "what to observe and how to adapt the decision to the actual conditions at home. ")
        extra = "Expensive materials are not necessary when available resources permit consistent and safe maintenance."
    blocks = []
    for index, (heading, topic) in enumerate(sections):
        blocks.append("# " + heading)
        for j in range(4):
            prefix = (f"Punto {index + 1}.{j + 1}: " if locale == "es" else f"Point {index + 1}.{j + 1}: ")
            warning = ("Advertencia importante: " if locale == "es" else "Important warning: ") if index == 5 and j == 1 else ""
            blocks.append(prefix + warning + topic.capitalize() + ". " + explain + extra)
    return "\n\n".join(blocks)


def test_realistic_fixture_is_long_and_safe():
    text = long_prose()
    assert answer_inline._units(text) > 4800
    assert len(summary_inline._passages(text)) == 24


@pytest.mark.parametrize("locale", ["es", "en"])
def test_long_prose_summary_covers_all_sections_and_is_substantial(locale):
    pages = answer_inline.InlinePages()
    k = pages.key(91, "alice@example.test", "test")
    text = long_prose(locale)
    question = "Explícame cómo cuidar una huerta en casa" if locale == "es" else "Explain how to care for a home garden"
    result, altered = pages.start(k, {"text": text, "sig": "model"}, locale, question)
    assert altered and result["text"] != text and result["sig"] == "model"
    assert 1400 <= answer_inline._units(result["text"]) <= answer_inline.CONTENT_UNITS
    assert ("Resumen de la respuesta (fragmentos):" if locale == "es" else "Answer summary (excerpts):") in result["text"]
    for title in (["La planificación", "El suelo", "El riego", "Las semillas", "El cuidado", "Las precauciones"] if locale == "es"
                  else ["Planning", "The soil", "Watering", "The seeds", "Care", "Precautions"]):
        assert title in result["text"]
    assert ("Advertencia importante" if locale == "es" else "Important warning") in result["text"]
    assert ("puede omitir detalles" if locale == "es" else "can omit details") in result["text"]


@pytest.mark.parametrize("locale", ["es", "en"])
def test_summary_expands_to_exact_original_and_retries_do_not_skip(locale):
    pages = answer_inline.InlinePages()
    k = pages.key(9, "alice@example.test", "private")
    text = long_prose(locale)
    summary, yes = pages.start(k, {"text": text}, locale, "huerta")
    assert yes
    first = pages.take(k, history(summary["text"]), locale)
    assert first and not first["text"].startswith(("Resumen de", "Answer summary"))
    assert pages.take(k, history(summary["text"]), locale) == first
    collected, visible = [raw_without_note(first["text"])], first["text"]
    while pages.status(k, history(visible)) == "ready":
        item = pages.take(k, history(visible), locale)
        collected.append(raw_without_note(item["text"]))
        visible = item["text"]
    assert "".join(collected) == text
    assert pages.status(k, history(visible)) == "none"


@pytest.mark.parametrize("locale", ["es", "en"])
def test_model_cut_is_disclosed_in_summary(locale):
    pages = answer_inline.InlinePages()
    result, yes = pages.start(pages.key(7, "a@example.test", "s"), {"text": long_prose(locale), "truncated": True}, locale)
    assert yes
    assert ("generación se cortó" if locale == "es" else "Generation was cut short") in result["text"]
    assert ("lo que sí se generó" if locale == "es" else "generated text") in result["text"]


@pytest.mark.parametrize("insert", ["\n\n```python\nprint(1)\n```", "\n\nhttps://example.test/source",
                                    "\n\n[S1] attribution", "\n\n| one | two |\n| --- | --- |",
                                    "\n\n$ sudo systemctl status service", "\n\nSELECT * FROM records", "\n\nprint(42)",
                                    "\n\n{\"enabled\": true}", "\n\n<article>example</article>", "\n\n[link](local/path)",
                                    "\n\nUsa `rm -rf` únicamente como un ejemplo."])
def test_code_sources_tables_and_commands_are_paged_not_summarized(insert):
    pages = answer_inline.InlinePages()
    k = pages.key(8, "a@example.test", "s")
    text = long_prose() + insert
    assert summary_inline.extract(text, "huerta") is None
    result, yes = pages.start(k, {"text": text}, "es", "huerta")
    assert yes and not result["text"].startswith("Resumen de")
    assert raw_without_note(result["text"]) == text[:len(raw_without_note(result["text"]))]


@pytest.mark.parametrize("term", ["amplía", "amplíalo", "amplía la explicación", "desarrolla", "ampliar", "expand", "expand on that", "elaborate"])
def test_natural_expansion_requests_recover_saved_text(term):
    assert answer_inline.is_continue(term)
    assert answer_inline.is_explicit_continue(term)


@pytest.mark.parametrize("term", ["amplía la segunda sección", "desarrolla otro tema", "expand on gardening only"])
def test_targeted_requests_are_not_hijacked(term):
    assert not answer_inline.is_continue(term)


def test_missing_summary_after_restart_reports_loss_without_fabricating():
    pages = answer_inline.InlinePages()
    k = pages.key(99, "a@example.test", "space")
    summary, yes = pages.start(k, {"text": long_prose()}, "es", "huerta")
    assert yes and summary["text"].startswith("Resumen de")
    restarted = answer_inline.InlinePages()
    status, version = restarted.reserve(k, history(summary["text"]))
    assert status == "unavailable" and version is not None
    warning = restarted.take(k, history(summary["text"]), "es", expected=version)
    assert "ya no está disponible" in warning["text"]


def test_summary_is_isolated_by_user_and_scope():
    pages = answer_inline.InlinePages()
    alice = pages.key(99, "alice@example.test", "verified-a")
    summary, _ = pages.start(alice, {"text": long_prose()}, "es", "huerta")
    for other in [pages.key(99, "bob@example.test", "verified-a"), pages.key(99, "alice@example.test", "verified-b")]:
        status, version = pages.reserve(other, history(summary["text"]))
        assert status == "unavailable"
        result = pages.take(other, history(summary["text"]), "es", expected=version)
        assert "ya no está disponible" in result["text"] and "Punto 1." not in result["text"]
    assert pages.status(alice, history(summary["text"])) == "ready"


def test_miniapp_uses_summary_and_expands_without_a_second_model_call(request, monkeypatch):
    from michiflin import miniapp
    entry, _pages, replies, generated, sent = request.getfixturevalue("integration")
    def ask(_chat, args, _locale):
        generated.append(args)
        return {"text": long_prose(), "sig": "IA local", "actions": []}
    monkeypatch.setitem(miniapp.OPS, "ask", ask)
    miniapp.handle(91, entry, {"id": "q1", "op": "ask", "lang": "es", "args": {"space": "agent", "text": "explica una huerta"}})
    summary = replies[-1][0]["text"]
    assert summary.startswith("Resumen de la respuesta") and len(generated) == 1
    miniapp.handle(91, entry, {"id": "q2", "op": "ask", "lang": "es", "args": {"space": "agent", "text": "amplía", "history": history(summary)}})
    assert replies[-1][0]["text"].startswith("# La planificación")
    assert len(generated) == 1 and not sent


def test_extraction_does_not_invoke_a_model_or_network(monkeypatch):
    import socket
    from michiflin import local_ai
    def forbidden(*_args, **_kwargs):
        raise AssertionError("summary must never call a model or network")
    monkeypatch.setattr(local_ai, "complete", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert summary_inline.extract(long_prose(), "huerta")


def test_extraction_of_bounded_text_is_fast_enough_for_the_shared_paginator():
    text = long_prose() * 8
    start = time.perf_counter()
    for _ in range(3):
        assert summary_inline.extract(text, "huerta")
    assert (time.perf_counter() - start) / 3 < 0.75


def test_multiple_explicit_warnings_are_preserved_or_the_answer_is_not_summarized():
    text = long_prose().replace("Punto 1.2:", "Punto 1.2: Advertencia esencial sobre la exposición.")
    text = text.replace("Punto 3.3:", "Punto 3.3: Precaución especial con el uso del agua.")
    text = text.replace("Punto 5.4:", "Punto 5.4: Riesgo importante para las mascotas.")
    result = summary_inline.extract(text, "huerta")
    if result:
        for phrase in ("Advertencia esencial", "Precaución especial", "Riesgo importante"):
            assert phrase in result
    else:
        # Conservative fallback always shows the whole answer in exact parts.
        pages = answer_inline.InlinePages()
        first, paged = pages.start(pages.key(92, "a@example.test", "s"), {"text": text}, "es")
        assert paged and not first["text"].startswith("Resumen")


def test_excessive_warnings_are_not_silently_dropped():
    text = long_prose().replace("Punto ", "Advertencia importante, punto ")
    assert summary_inline.extract(text, "huerta") is None


def test_numbered_repetition_is_not_duplicated_in_the_summary():
    text = long_prose()
    result = summary_inline.extract(text, "huerta")
    assert result and "Punto 2.1" in result and "Punto 2.2" not in result


def test_dependent_followup_is_not_separated_from_its_context():
    chunks = [
        "La medida inicial debe adaptarse a las condiciones concretas del lugar. " * 3,
        "Sin embargo, no debe aplicarse cuando el suelo está saturado de agua. " * 2,
    ]
    paragraphs = ["Introducción detallada sobre el cuidado de una huerta con condiciones variables. " * 3]
    paragraphs.extend(chunks)
    paragraphs += [f"Párrafo {n}: " + "Conviene revisar la tierra y las hojas con calma para tomar decisiones útiles. " * 4 for n in range(20)]
    text = "\n\n".join(paragraphs)
    result = summary_inline.extract(text, "huerta")
    if result and "Sin embargo" in result:
        assert "La medida inicial" in result


def test_summary_expiry_after_both_memory_ttls_is_still_honest():
    timepoint = [0.0]
    pages = answer_inline.InlinePages(ttl=10, clock=lambda: timepoint[0])
    k = pages.key(99, "a@example.test", "s")
    summary, _ = pages.start(k, {"text": long_prose()}, "es", "huerta")
    timepoint[0] = 11
    pages.prune()  # drop the private original, retain a text-free tombstone
    timepoint[0] = 22
    pages.prune()  # no tombstone remains; the app's own receipt is sufficient
    status, version = pages.reserve(k, history(summary["text"]))
    assert status == "unavailable"
    assert "ya no está disponible" in pages.take(k, history(summary["text"]), "es", expected=version)["text"]


@pytest.mark.parametrize("text", ["[" * 110_000, "<a" * 55_000, "[" * 127 + "x" * 100_000])
def test_adversarial_markup_is_bounded_and_falls_back_quickly(text):
    start = time.perf_counter()
    assert summary_inline.extract(text, "test") is None
    assert time.perf_counter() - start < 0.75


def test_miniapp_warns_after_restart_instead_of_inventing_the_pending_original(request, monkeypatch):
    from michiflin import miniapp
    entry, _pages, replies, generated, sent = request.getfixturevalue("integration")
    def ask(_chat, args, _locale):
        generated.append(args)
        return {"text": long_prose(), "sig": "IA local", "actions": []}
    monkeypatch.setitem(miniapp.OPS, "ask", ask)
    miniapp.handle(91, entry, {"id": "before-restart", "op": "ask", "lang": "es", "args": {"space": "agent", "text": "huerta"}})
    summary = replies[-1][0]["text"]
    assert summary.startswith("Resumen de la respuesta")
    monkeypatch.setattr(miniapp, "_inline_pages", answer_inline.InlinePages())
    miniapp.handle(91, entry, {"id": "after-restart", "op": "ask", "lang": "es", "args": {
        "space": "agent", "text": "amplía", "history": history(summary)}})
    assert "ya no está disponible" in replies[-1][0]["text"]
    assert len(generated) == 1 and not sent
