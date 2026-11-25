"""The manual must describe the current code and not omit Python modules."""
import ast
import re
from pathlib import Path

from pypdf import PdfReader


def test_manual_describes_every_current_python_module():
    root = Path(__file__).resolve().parents[1]
    source = (root / "tools" / "build_manual.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "MODULES" for target in item.targets))
    descriptions = ast.literal_eval(node.value)
    modules = {path.name for path in (root / "michiflin").glob("*.py")}
    assert set(descriptions) == modules
    assert all(isinstance(description, str) and description.strip() for description in descriptions.values())


def test_manual_pdf_and_generator_describe_countdown_and_reader_navigation():
    root = Path(__file__).resolve().parents[1]
    source = (root / 'tools' / 'build_manual.py').read_text(encoding='utf-8')
    phrases = ('tiempo estimado hasta el reinicio del bot', 'Biblioteca y lectura', 'márgenes exteriores', 'Los enlaces internos EPUB')
    assert all(phrase in source for phrase in phrases)
    # The separately delivered source ZIP intentionally omits the PDF.
    manual = root / 'docs' / 'Manual_Michiflin.pdf'
    if manual.exists():
        pages = PdfReader(str(manual)).pages
        text = re.sub(r'\s+', ' ', ' '.join(page.extract_text() or '' for page in pages))
        assert all(phrase in text for phrase in phrases)
        assert 'El primer banner de Inicio saluda según la hora local' not in text
