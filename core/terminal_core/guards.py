"""Garde-fous statiques partagés par les suites de tests des projets du terminal."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path


def _is_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def insecure_tls_uses(paths: Iterable[Path]) -> list[str]:
    """Emplacements ``fichier:ligne`` qui désactivent la vérification TLS.

    Détecte ``f(..., verify=False)``, ``x.verify = False`` et ``ssl._create_unverified_context``,
    par analyse syntaxique (les commentaires et docstrings ne comptent pas).
    """
    hits: list[str] = []
    for root in paths:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if any(k.arg == "verify" and _is_false(k.value) for k in node.keywords):
                        hits.append(f"{path}:{node.lineno}")
                    func = node.func
                    if isinstance(func, ast.Attribute) and func.attr == "_create_unverified_context":
                        hits.append(f"{path}:{node.lineno}")
                elif isinstance(node, ast.Assign) and _is_false(node.value):
                    if any(isinstance(t, ast.Attribute) and t.attr == "verify" for t in node.targets):
                        hits.append(f"{path}:{node.lineno}")
    return sorted(set(hits), key=lambda h: (h.rsplit(":", 1)[0], int(h.rsplit(":", 1)[1])))
