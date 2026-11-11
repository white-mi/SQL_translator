"""Every module, class and function says what it is and how to use it.

The rule from CLAUDE.md, checked statically: each module, class, function, method and nested
function in the repository's Python code has a docstring. Test functions (`test_*` at the top
level of a test module) are exempt, because their names are sentences that state what they check.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("sqltr", "case", "tests")


def python_files() -> list[Path]:
    """Return the repository's Python files, skipping hidden directories and caches."""
    files = [ROOT / "conftest.py"]
    for top in SOURCE_DIRS:
        for path in sorted((ROOT / top).rglob("*.py")):
            parts = path.relative_to(ROOT).parts
            if not any(p.startswith(".") or p == "__pycache__" for p in parts):
                files.append(path)
    return files


def undocumented(path: Path) -> list[str]:
    """Return `line:qualified.name` for every definition in `path` that lacks a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    test_module = path.name.startswith("test_") or path.name == "conftest.py"
    missing = [] if ast.get_docstring(tree) else ["1:<module>"]

    def walk(node: ast.AST, prefix: str) -> None:
        """Visit the definitions directly inside `node`, then recurse into each of them."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                exempt = test_module and not prefix and child.name.startswith("test_")
                if not exempt and not ast.get_docstring(child):
                    missing.append(f"{child.lineno}:{prefix}{child.name}")
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")
    return missing


def test_every_definition_has_a_docstring():
    gaps = {str(p.relative_to(ROOT)): m for p in python_files() if (m := undocumented(p))}
    assert not gaps, f"add docstrings (what it is, how to use it): {gaps}"
