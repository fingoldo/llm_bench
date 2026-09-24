"""llm_bench's package layering, checked by ``py_ci_shared.import_layering``.

The intended dependency stack:

    core/        (no internal imports: leaf layer)
      ^
    pool/, stage/, cost/, storage/, provider/   (depend on core/ only)
      ^
    halving/, ranking/, discovery/   (depend on the layer above + core)
      ^
    runner/, confirmation/           (depend on everything else)
      ^
    cli/

A subpackage may import only from a LOWER layer; siblings at one layer may not import each other. The shared checker
resolves relative imports too (``from ..runner import x``), which the local scanner it replaces did not. Real import
cycles, including the load-order kind, are the ``import_cycles`` gate in ``[tool.py_ci_shared]`` of pyproject.toml.
"""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.import_layering import LayerRule, assert_layering, find_layering_violations

REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG = "src/llm_bench"

# Layer index: lower = more foundational.
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


def _rules() -> list[LayerRule]:
    return [
        LayerRule(
            f"{_PKG}/{sub}/*",
            [f"{_PKG}/{other}/*" for other, other_layer in _LAYER.items() if other != sub and other_layer >= layer],
            reason=f"{sub}/ is layer {layer}: it may import only from lower layers",
        )
        for sub, layer in _LAYER.items()
    ]


def test_every_layer_exists():
    missing = [sub for sub in _LAYER if not (REPO_ROOT / _PKG / sub).is_dir()]
    assert not missing, f"layers named in _LAYER with no directory under {_PKG}: {missing}"


def test_no_upward_or_sibling_imports():
    assert_layering(REPO_ROOT, _rules())


def test_an_upward_import_is_reported(tmp_path):
    """The rules have teeth on this layout: core importing runner is a violation."""
    for sub in ("core", "runner"):
        (tmp_path / _PKG / sub).mkdir(parents=True)
        (tmp_path / _PKG / sub / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / _PKG / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / _PKG / "runner" / "r.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / _PKG / "core" / "c.py").write_text("from llm_bench.runner.r import X\n", encoding="utf-8")
    problems = find_layering_violations(tmp_path, _rules())
    assert any("core/c.py" in p and "runner" in p for p in problems), problems
