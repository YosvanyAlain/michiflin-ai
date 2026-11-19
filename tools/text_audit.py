"""Herramienta de taller: mide el texto de la interfaz.

No forma parte del bot. Lista las cadenas mas largas de i18n.js, cuenta
cuantas superan el umbral de lectura comoda y agrupa las que mencionan
servicios externos (YouTube, ETECSA/Nauta) para ver si el aviso se repite
en sitios distintos en vez de vivir en una sola seccion.

Uso: python3 tools/text_audit.py [--top 25] [--limit 160]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

I18N = Path(__file__).resolve().parent.parent / "webxdc" / "i18n.js"
APP = Path(__file__).resolve().parent.parent / "webxdc" / "app.js"


def load(language: str) -> dict[str, str]:
    """Lee I18N con node, porque el fichero es JS y no JSON."""
    script = f"const s=require('fs').readFileSync({str(I18N)!r},'utf8');" \
             "const I18N=new Function(s+'; return I18N;')();" \
             f"process.stdout.write(JSON.stringify(I18N[{language!r}]));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def used_keys() -> set[str]:
    src = APP.read_text(encoding="utf-8")
    return set(re.findall(r"""t\(\s*["']([a-zA-Z0-9_.]+)["']""", src))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", default="es")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--limit", type=int, default=160, help="umbral de lectura comoda")
    options = parser.parse_args()

    strings = load(options.language)
    lengths = sorted(((len(v), k, v) for k, v in strings.items() if isinstance(v, str)), reverse=True)
    over = [item for item in lengths if item[0] > options.limit]

    total = sum(n for n, _, _ in lengths)
    print(f"\nTexto de la interfaz ({options.language}): {len(strings)} claves, {total} caracteres")
    print(f"por encima de {options.limit} caracteres: {len(over)} claves "
          f"({sum(n for n, _, _ in over)} caracteres, {sum(n for n,_,_ in over)*100//max(total,1)}% del total)\n")

    print(f"{'clave':<34}{'car.':>6}  inicio")
    print("-" * 110)
    for n, key, value in lengths[:options.top]:
        print(f"{key:<34}{n:>6}  {value[:58]}...")

    groups = {
        "YouTube": re.compile(r"youtube|yt\b", re.I),
        "ETECSA / Nauta": re.compile(r"etecsa|nauta", re.I),
        "politica / terminos": re.compile(r"pol[ií]tic|t[eé]rmino|condicion|derechos de autor|copyright", re.I),
    }
    print()
    for label, pattern in groups.items():
        hits = [(k, len(v)) for k, v in strings.items() if isinstance(v, str) and pattern.search(v)]
        chars = sum(n for _, n in hits)
        print(f"{label:<22} {len(hits):>3} claves, {chars:>5} caracteres")
        for key, n in sorted(hits, key=lambda x: -x[1])[:6]:
            print(f"    {key:<32}{n:>5}")

    live = used_keys()
    huerfanas = [k for k, _ in ((k, v) for k, v in strings.items()) if k not in live]
    print(f"\nclaves no referenciadas directamente en app.js: {len(huerfanas)} "
          f"(muchas se construyen por prefijo, no es error por si solo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
