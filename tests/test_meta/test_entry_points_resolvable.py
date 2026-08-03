"""Verify every ``[project.scripts]`` / ``[project.entry-points.*]``
console-script target in ``pyproject.toml`` resolves to a real,
importable ``module:attr`` pair.

Motivating finding: ``pyproject.toml`` declared ``llm-bench =
llm_bench.cli.main:main`` before ``src/llm_bench/cli/main.py`` existed --
a fresh ``pip install`` followed by running ``llm-bench`` crashed with
``ModuleNotFoundError``. ``cli/main.py`` (and its ``main()`` function)
exist now, but nothing enforced the pyproject.toml <-> source-tree
contract, so a future rename/move of the target module or function would
silently reintroduce the same crash for every fresh install. This is a
static resolvability check (import + getattr), not a full CLI smoke test
of what ``main()`` actually does at runtime.

No ``_WHITELIST`` / baseline here: an entry point that fails to import or
is missing its target attribute is unconditionally broken for every
consumer -- there is no legitimate-false-positive case to carry forward,
so this is a hard assert-empty.
"""

from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path

import pytest

_PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"

# Sentinel distinguishing "attribute missing" from "attribute is None"
# (a legitimate value getattr's own default can't be used for).
_MISSING = object()


def _iter_entry_point_specs(pyproject: dict) -> list[tuple[str, str]]:
    """Return ``(qualified_name, "module:attr")`` pairs from both
    ``[project.scripts]`` and any ``[project.entry-points.*]`` tables."""
    project = pyproject.get("project", {})
    specs: list[tuple[str, str]] = []
    for name, target in project.get("scripts", {}).items():
        specs.append((f"project.scripts.{name}", target))
    for group, mapping in project.get("entry-points", {}).items():
        for name, target in mapping.items():
            specs.append((f"project.entry-points.{group}.{name}", target))
    return specs


def _check_entry_point(qualified_name: str, target: str) -> str | None:
    """Return a violation string if ``target`` ('module:attr') fails to
    resolve via import + getattr, else None."""
    if ":" not in target:
        return f"{qualified_name} = {target!r}: malformed entry point, expected 'module:attr'."
    module_name, _, attr_path = target.partition(":")
    try:
        obj: object = importlib.import_module(module_name)
    except Exception as e:
        return f"{qualified_name} = {target!r}: import_module({module_name!r}) raised " f"{type(e).__name__}: {e}"
    # Support a dotted attr path (e.g. "module:Class.method"), not just a
    # single top-level name, since that's a valid entry-point shape too.
    try:
        for part in attr_path.split("."):
            obj = getattr(obj, part, _MISSING)
            if obj is _MISSING:
                return f"{qualified_name} = {target!r}: {module_name!r} has no attribute " f"path {attr_path!r} (missing at {part!r})."
    except Exception as e:
        # 3-arg getattr only swallows AttributeError; a module/object with a
        # __getattr__ (PEP 562 lazy-import shims, deprecation stubs, etc.)
        # can raise anything else during attribute resolution. Must be
        # caught here too, mirroring the import_module try/except above --
        # otherwise a broken entry point of this shape crashes this test
        # with a raw traceback instead of a clean, actionable violation.
        return f"{qualified_name} = {target!r}: attribute access on {module_name!r} raised " f"{type(e).__name__}: {e}"
    return None


def _scan(pyproject_path: Path) -> list[str]:
    with pyproject_path.open("rb") as f:
        pyproject = tomllib.load(f)
    violations: list[str] = []
    for qualified_name, target in _iter_entry_point_specs(pyproject):
        violation = _check_entry_point(qualified_name, target)
        if violation is not None:
            violations.append(violation)
    return violations


def test_entry_points_resolvable():
    violations = _scan(_PYPROJECT_PATH)
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"Unresolvable console-script entry point(s) in pyproject.toml:\n  {msg}")


class TestScanDetectsEntryPointShapes:
    """Direct regression tests for the checker itself, against synthetic
    pyproject.toml files via tmp_path (``_scan`` reads from disk, not a
    string) -- one proving it FIRES on a broken entry point, one proving
    it stays silent on a resolvable one. Uses only stdlib targets
    (os.path) so these tests don't depend on llm_bench's own layout."""

    def _write(self, tmp_path: Path, toml_text: str) -> Path:
        p = tmp_path / "pyproject.toml"
        p.write_text(toml_text, encoding="utf-8")
        return p

    def test_missing_module_detected(self, tmp_path):
        path = self._write(
            tmp_path,
            '[project.scripts]\nbroken-cmd = "llm_bench_module_does_not_exist_xyz.cli:main"\n',
        )
        violations = _scan(path)
        assert len(violations) == 1
        assert "broken-cmd" in violations[0]

    def test_missing_attribute_detected(self, tmp_path):
        path = self._write(
            tmp_path,
            '[project.scripts]\nbroken-cmd = "os.path:this_attr_does_not_exist_xyz"\n',
        )
        violations = _scan(path)
        assert len(violations) == 1
        assert "broken-cmd" in violations[0]

    def test_malformed_spec_detected(self, tmp_path):
        path = self._write(
            tmp_path,
            '[project.scripts]\nbroken-cmd = "no_colon_here"\n',
        )
        violations = _scan(path)
        assert len(violations) == 1
        assert "malformed" in violations[0]

    def test_resolvable_entry_point_not_flagged(self, tmp_path):
        path = self._write(
            tmp_path,
            '[project.scripts]\nok-cmd = "os.path:join"\n',
        )
        violations = _scan(path)
        assert violations == []

    def test_resolvable_dotted_attr_not_flagged(self, tmp_path):
        path = self._write(
            tmp_path,
            '[project.entry-points."console_scripts"]\nok-cmd = "os:path.join"\n',
        )
        violations = _scan(path)
        assert violations == []

    def test_getattr_raising_non_attribute_error_detected(self, tmp_path, monkeypatch):
        """A module implementing a PEP 562 module-level ``__getattr__`` that
        raises something other than AttributeError (e.g. a lazy-import /
        deprecation shim -- an increasingly common real-world pattern) must
        still produce a clean violation string instead of an uncaught
        exception escaping ``_scan``. Regression for the getattr walk's own
        try/except, which must mirror the import_module try/except above it."""
        mod_name = "llm_bench_test_lazy_getattr_shim"
        src_dir = tmp_path / "shim_src"
        src_dir.mkdir()
        (src_dir / f"{mod_name}.py").write_text(
            "def __getattr__(name):\n    raise RuntimeError(f'lazy load of {name!r} failed')\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(src_dir))
        path = self._write(
            tmp_path,
            f'[project.scripts]\nbroken-cmd = "{mod_name}:main"\n',
        )
        try:
            violations = _scan(path)
        finally:
            sys.modules.pop(mod_name, None)
        assert len(violations) == 1
        assert "broken-cmd" in violations[0]
        assert "RuntimeError" in violations[0]
