"""The manual must not silently omit newly added Python modules."""
import ast
from pathlib import Path


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
