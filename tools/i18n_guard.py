"""Check that no interface string the tests rely on has disappeared.

Not part of the bot. Shortening interface copy silently breaks assertions that
look for a phrase on screen, and `node --check` cannot see it. Pairing quotes
with a regex over a whole file is wrong — one apostrophe shifts every pair
after it — so the literals are taken from the Python AST instead.

Usage: python3 tools/i18n_guard.py [--base HEAD]
"""
from __future__ import annotations

import argparse
import ast
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# literales que coinciden por casualidad: son datos del test, no texto de la interfaz
EXENTOS = {"sin respuesta", "A result"}  # datos de prueba, no texto de la interfaz


def literales(ruta: Path) -> set[str]:
    try:
        arbol = ast.parse(ruta.read_text(), filename=str(ruta))
    except SyntaxError:
        return set()
    out = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            texto = nodo.value.strip()
            if 8 <= len(texto) <= 120 and " " in texto:
                out.add(texto)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="HEAD")
    opciones = parser.parse_args()

    actual = (RAIZ / "webxdc" / "i18n.js").read_text()
    previo = subprocess.run(
        ["git", "show", f"{opciones.base}:webxdc/i18n.js"],
        capture_output=True, text=True, cwd=RAIZ,
    ).stdout
    if not previo:
        print(f"no se pudo leer i18n.js en {opciones.base}")
        return 2

    rotos = []
    for fichero in sorted((RAIZ / "tests").glob("*.py")):
        for texto in literales(fichero):
            if texto in EXENTOS:
                continue
            if texto in previo and texto not in actual:
                rotos.append((fichero.name, texto))

    if rotos:
        print(f"literales que los tests esperan y el recorte eliminó ({len(rotos)}):\n")
        for nombre, texto in sorted(set(rotos)):
            print(f"  {nombre}: {texto!r}")
        return 1
    print(f"sin pérdidas frente a {opciones.base}: ningún literal esperado por tests desapareció")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
