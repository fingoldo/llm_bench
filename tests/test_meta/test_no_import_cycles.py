"""Catch import cycles in llm_bench's package layering.

The framework's intended dependency stack:

    core/        (no internal imports — leaf layer)
      ↑
    pool/, stage/, cost/, storage/   (depend on core/ only)
      ↑
    halving/, ranking/, discovery/   (depend on the layer above + core)
      ↑
    runner/, confirmation/, cli/     (depend on everything else)

Any reverse edge — e.g. ``core/`` importing from ``runner/`` — would
introduce a cycle and is rejected here. The check is structural (AST
parse of every .py file under src/llm_bench/) so it doesn't require
the modules to actually import (handy when an optional dep is missing).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench"

# Layer index — lower = more foundational. Imports may only flow from
# higher index → lower index. Equal-index siblings may NOT import each
# other (sibling discipline keeps the layout flat).
_LAYER: dict[str, int] = {
    "core": 0,
    "pool": 1,
    "stage": 1,
    "cost": 1,
    "storage": 1,
    "provider": 1,
    "halving": 2,
    "ranking": 2,
    "discovery": 2,
    "confirmation": 3,
    "runner": 3,
    "cli": 4,
}


def _module_layer(rel: Path) -> tuple[str, int] | None:
    """For ``src/llm_bench/storage/memory.py`` returns ('storage', 1)."""
    parts = rel.parts
    # parts[0] is the subpackage name; "__init__.py" or any submodule.
    if len(parts) < 1:
        return None
    sub = parts[0]
    if sub not in _LAYER:
        return None
    return sub, _LAYER[sub]


def _imports_from_module(path: Path) -> set[str]:
    """Return every ``llm_bench.X`` (where X is a known subpackage)
    imported by this file."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        pytest.fail(f"SyntaxError parsing {path}: {e}")
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("llm_bench."):
                # llm_bench.storage.base -> 'storage'
                head = mod.split(".", 2)[1]
                if head in _LAYER:
                    out.add(head)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("llm_bench."):
                    head = alias.name.split(".", 2)[1]
                    if head in _LAYER:
                        out.add(head)
    return out


def test_no_upward_imports():
    """Layer-N module may NOT import from layer >= N+1 (no upward edges,
    no sibling-to-sibling)."""
    violations: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(_SRC_ROOT)
        info = _module_layer(rel)
        if info is None:
            continue
        sub, layer = info
        for imported in _imports_from_module(path):
            if imported == sub:
                continue  # intra-subpackage imports OK
            other_layer = _LAYER[imported]
            if other_layer >= layer:
                # Sibling (==) or upward (>) — both forbidden.
                kind = "sibling" if other_layer == layer else "upward"
                violations.append(
                    f"{rel}: {kind} import from {imported!r} "
                    f"(layer {other_layer}) — this module is layer {layer}. "
                    f"Either move the shared code to a lower layer, or "
                    f"restructure to remove the dependency."
                )
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"Import-layer violations:\n  {msg}")
