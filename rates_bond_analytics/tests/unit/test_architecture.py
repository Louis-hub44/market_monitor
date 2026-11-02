"""Architecture guards: the engine must not depend on the UI nor on the test oracle."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "rates_analytics"
FORBIDDEN = {"streamlit", "QuantLib", "plotly"}  # plotly allowed only in the UI layer


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(SRC)))
def test_engine_has_no_ui_or_oracle_imports(path):
    assert not (_imports(path) & FORBIDDEN), path


def test_no_hardcoded_api_keys():
    pattern = re.compile(r"(api_?key)\s*[=:]\s*['\"]?[A-Za-z0-9]{16,}", re.IGNORECASE)
    for path in list(SRC.rglob("*.py")) + list((SRC.parents[1] / "config").glob("*.yaml")):
        assert not pattern.search(path.read_text(encoding="utf-8")), path
