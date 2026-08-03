"""Detect functions/methods/properties callable ONLY from their own
test/example, never from actual production code (test-only-symbol /
unwired-capability smell).

Motivating audit finding: this single shape explained 8 separate findings in
the original full-repo audit -- functions/fields DOCUMENTED as orchestration-
critical but wired up only in their own unit test, never reachable from the
real runner call graph (per-stage-winner promotion, validator independence,
StageContext.quarantined, Stage.budget_per_call, ...). All eight were fixed
during the main remediation pass; this is the regression guard that catches
a relapse, or a new instance of the same mistake.

Coverage note: this scan only sees ``FunctionDef``/``AsyncFunctionDef``
nodes (plain functions, methods, and ``@property``/``@cached_property``
methods) and their ``ast.Call`` / bare-attribute-access sites. Two of the
eight originally-cited findings -- ``StageContext.quarantined`` and
``Stage.budget_per_call`` -- are plain dataclass fields, not functions, and
are therefore structurally out of scope for this checker; a relapse in a
plain field's wiring would not be caught here.

Algorithm (matches the audit's description of the shape):

  1. Collect every top-level function and every method (including
     properties) defined under ``src/llm_bench`` whose name doesn't start
     with "_". Every such symbol is a candidate, including ones re-exported
     via an ``__init__.py``'s ``__all__`` -- a docstring-substring gate on
     ``__all__`` membership was tried and dropped: it silently excluded the
     production hooks it was meant to protect (e.g. any orchestration
     docstring not matching one of a fixed phrase list) before call-site
     analysis ever ran, defeating the point of the guard. Legitimate public
     helpers that happen to be called only from tests/examples are instead
     suppressed via the baseline ratchet in step 4, same as every other
     false-positive in this file.
  2. Resolve every ``ast.Call`` site across src/, tests/, and examples/ by
     its bare name (``ast.Name.id`` / ``ast.Attribute.attr`` -- no type
     resolution, matching the audit's own description of the check).
     Additionally, for symbols decorated with ``@property`` or
     ``@cached_property``, resolve bare attribute-access sites (``obj.name``
     with no call parens), since a property is read, not called.
  3. Bucket each site as "src" or "tests+examples".
  4. Flag any surviving candidate whose sites exist ONLY in the
     tests+examples bucket.

Name-based call resolution is approximate: a common method name shared by
two unrelated classes can hide a real miss, and more often produces a
plausible-looking hit that's actually a legitimate public helper (any
function meant for external callers looks "test-only" from inside this one
repo). Given that real false-positive risk, findings are baseline-ratcheted
(same pattern as test_code_audit_baseline.py / test_api_stability.py)
rather than hard-failing on any finding -- only a NEW finding fails the
test. Refresh after a reviewed, intentional change::

    pytest tests/test_meta/test_unwired_capabilities.py --refresh-unwired-capabilities-baseline
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src" / "llm_bench"
_TESTS_ROOT = _REPO_ROOT / "tests"
_EXAMPLES_ROOT = _REPO_ROOT / "examples"

_BASELINE_PATH = Path(__file__).resolve().parent / "_unwired_capabilities_baseline.json"
_REFRESH_FLAG = "--refresh-unwired-capabilities-baseline"

# Decorator names that mark a method as a property (read via bare attribute
# access, e.g. ``obj.name``, never via ``ast.Call``).
_PROPERTY_DECORATORS: frozenset[str] = frozenset({"property", "cached_property"})

# Per-symbol whitelist for confirmed legitimate public helpers that this
# name-based checker can't distinguish from a real miss. Format:
# "<rel_path>:<lineno>::<name>".
_WHITELIST: set[str] = set()


@dataclass(frozen=True)
class _Symbol:
    name: str
    rel_path: str
    lineno: int
    kind: str  # "function" | "method" | "property"


def _rel(path: Path, base: Path) -> str:
    return str(path.relative_to(base)).replace("\\", "/")


def _iter_py_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts]


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return None


def _is_property(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        dec_name = dec.id if isinstance(dec, ast.Name) else dec.attr if isinstance(dec, ast.Attribute) else None
        if dec_name in _PROPERTY_DECORATORS:
            return True
    return False


def _maybe_symbol(node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str, rel: str) -> _Symbol | None:
    if node.name.startswith("_"):
        return None
    if kind == "method" and _is_property(node):
        kind = "property"
    return _Symbol(name=node.name, rel_path=rel, lineno=node.lineno, kind=kind)


def _collect_candidate_symbols(src_root: Path, repo_root: Path) -> list[_Symbol]:
    """Top-level functions and methods (including properties) under
    ``src_root``, minus private names. Every public symbol is a candidate
    -- see module docstring, step 1, for why re-exported names are no
    longer pre-filtered by docstring wording."""
    symbols: list[_Symbol] = []
    for path in _iter_py_files(src_root):
        tree = _parse(path)
        if tree is None:
            continue
        rel = _rel(path, repo_root)

        # Top-level (module-scope) functions.
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sym = _maybe_symbol(node, "function", rel)
                if sym is not None:
                    symbols.append(sym)
        # Methods: direct children of any class body (any nesting depth).
        for cls_node in ast.walk(tree):
            if isinstance(cls_node, ast.ClassDef):
                for child in cls_node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        sym = _maybe_symbol(child, "method", rel)
                        if sym is not None:
                            symbols.append(sym)
    return symbols


def _collect_called_names(root: Path) -> set[str]:
    """Every ``ast.Call`` target's bare name (``foo(...)`` -> "foo",
    ``obj.foo(...)`` -> "foo") across every ``*.py`` file under ``root``."""
    names: set[str] = set()
    for path in _iter_py_files(root):
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _collect_accessed_attribute_names(root: Path) -> set[str]:
    """Every ``ast.Attribute.attr`` across every ``*.py`` file under
    ``root``, regardless of whether it's called -- used to resolve
    ``@property``/``@cached_property`` reads (``obj.name``, no call
    parens), which never appear as an ``ast.Call`` target."""
    names: set[str] = set()
    for path in _iter_py_files(root):
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


def _scan_unwired_capabilities(repo_root: Path, src_root: Path, tests_root: Path, examples_root: Path) -> list[str]:
    """Pure scan: return one violation string per test-only symbol found
    under ``src_root``, given sibling ``tests_root`` / ``examples_root``
    directories to resolve call/access sites against."""
    symbols = _collect_candidate_symbols(src_root, repo_root)

    src_called = _collect_called_names(src_root)
    test_called = _collect_called_names(tests_root) | _collect_called_names(examples_root)
    src_accessed = _collect_accessed_attribute_names(src_root)
    test_accessed = _collect_accessed_attribute_names(tests_root) | _collect_accessed_attribute_names(examples_root)

    violations: list[str] = []
    for sym in symbols:
        if sym.kind == "property":
            in_test_bucket, in_src_bucket = sym.name in test_accessed, sym.name in src_accessed
        else:
            in_test_bucket, in_src_bucket = sym.name in test_called, sym.name in src_called
        if in_test_bucket and not in_src_bucket:
            key = f"{sym.rel_path}:{sym.lineno}::{sym.name}"
            if key in _WHITELIST:
                continue
            violations.append(
                f"{sym.rel_path}:{sym.lineno}: {sym.kind} {sym.name!r} has call/access sites "
                f"only under tests/examples, none under src/ -- wire it into the real "
                f"call graph, or if it's a standalone public helper, add {key!r} to "
                f"_WHITELIST with a one-line reason."
            )
    return sorted(violations)


def _assert_no_new_findings(current: list[str]) -> None:
    current_set = set(current)
    if _REFRESH_FLAG in sys.argv or not _BASELINE_PATH.exists():
        _BASELINE_PATH.write_text(json.dumps(sorted(current_set), indent=2), encoding="utf-8")
        pytest.skip(
            f"unwired-capabilities baseline refreshed at {_BASELINE_PATH.name} "
            f"({len(current_set)} existing finding(s)). Re-run without the refresh flag to verify."
        )
        return

    baseline_set = set(json.loads(_BASELINE_PATH.read_text(encoding="utf-8")))
    new = sorted(current_set - baseline_set)
    if new:
        msg = "\n  ".join(new)
        pytest.fail(
            f"New test-only-symbol finding(s) (called only from tests/examples, never "
            f"src/ production code):\n  {msg}\n"
            f"Fix: wire the symbol into src/, or if it's a confirmed legitimate public "
            f"helper, refresh the baseline after review: "
            f"pytest tests/test_meta/test_unwired_capabilities.py {_REFRESH_FLAG}"
        )


def test_no_new_unwired_capabilities():
    current = _scan_unwired_capabilities(_REPO_ROOT, _SRC_ROOT, _TESTS_ROOT, _EXAMPLES_ROOT)
    _assert_no_new_findings(current)


class TestScanUnwiredCapabilitiesDetectsShapes:
    """Direct regression tests for ``_scan_unwired_capabilities`` itself,
    against synthetic ``src/<pkg>`` + ``tests`` + ``examples`` trees built
    under ``tmp_path`` (mirrors the real repo layout: three sibling roots
    under one ``repo_root``)."""

    def _make_tree(
        self, tmp_path: Path, *, src_files: dict[str, str], test_files: dict[str, str], example_files: dict[str, str] | None = None
    ) -> tuple[Path, Path, Path, Path]:
        src_root = tmp_path / "src" / "pkg"
        tests_root = tmp_path / "tests"
        examples_root = tmp_path / "examples"
        for rel, content in src_files.items():
            p = src_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        for rel, content in test_files.items():
            p = tests_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        for rel, content in (example_files or {}).items():
            p = examples_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return tmp_path, src_root, tests_root, examples_root

    def _run(self, tmp_path: Path, **kwargs) -> list[str]:
        repo_root, src_root, tests_root, examples_root = self._make_tree(tmp_path, **kwargs)
        return _scan_unwired_capabilities(repo_root, src_root, tests_root, examples_root)

    def test_fires_on_function_called_only_from_tests(self, tmp_path):
        violations = self._run(
            tmp_path,
            src_files={
                "__init__.py": "",
                "mod.py": "def orphan_function():\n    return 42\n",
            },
            test_files={
                "test_x.py": "from pkg.mod import orphan_function\n\n\ndef test_it():\n    assert orphan_function() == 42\n",
            },
        )
        assert len(violations) == 1
        assert "orphan_function" in violations[0]

    def test_silent_when_also_called_from_src(self, tmp_path):
        violations = self._run(
            tmp_path,
            src_files={
                "__init__.py": "",
                "mod.py": ("def wired_function():\n    return 1\n\n\ndef caller():\n    return wired_function()\n"),
            },
            test_files={
                "test_x.py": "from pkg.mod import wired_function\n\n\ndef test_it():\n    assert wired_function() == 1\n",
            },
        )
        assert violations == []

    def test_fires_for_dunder_all_export_called_only_from_tests(self, tmp_path):
        # __all__ re-export is no longer a candidacy gate: a symbol
        # exported for external consumers but currently called only from
        # tests still fires here and relies on the baseline/_WHITELIST to
        # suppress it once reviewed as legitimate (module docstring, step
        # 1) -- this is what closes the gap where a flagship orchestration
        # hook wasn't scanned at all just because it was exported and its
        # docstring didn't match one of a fixed phrase list.
        violations = self._run(
            tmp_path,
            src_files={
                "__init__.py": ('from pkg.mod import public_helper\n\n__all__ = ["public_helper"]\n'),
                "mod.py": ('def public_helper():\n    """A plain reusable helper for external callers."""\n    return 1\n'),
            },
            test_files={
                "test_x.py": "from pkg.mod import public_helper\n\n\ndef test_it():\n    assert public_helper() == 1\n",
            },
        )
        assert len(violations) == 1
        assert "public_helper" in violations[0]

    def test_fires_for_dunder_all_export_with_orchestration_docstring(self, tmp_path):
        violations = self._run(
            tmp_path,
            src_files={
                "__init__.py": ('from pkg.mod import critical_helper\n\n__all__ = ["critical_helper"]\n'),
                "mod.py": ('def critical_helper():\n    """Promotes stage winners; the runner calls this after every round."""\n    return 1\n'),
            },
            test_files={
                "test_x.py": "from pkg.mod import critical_helper\n\n\ndef test_it():\n    assert critical_helper() == 1\n",
            },
        )
        assert len(violations) == 1
        assert "critical_helper" in violations[0]

    def test_property_fires_when_accessed_only_from_tests(self, tmp_path):
        violations = self._run(
            tmp_path,
            src_files={
                "__init__.py": "",
                "mod.py": (
                    'class Foo:\n    @property\n    def only_read_in_tests(self):\n        """The runner calls this after every round."""\n        return 1\n'
                ),
            },
            test_files={
                "test_x.py": ("from pkg.mod import Foo\n\n\ndef test_it():\n    f = Foo()\n    assert f.only_read_in_tests == 1\n"),
            },
        )
        assert len(violations) == 1
        assert "only_read_in_tests" in violations[0]
        assert "property 'only_read_in_tests'" in violations[0]

    def test_property_silent_when_also_read_from_src(self, tmp_path):
        violations = self._run(
            tmp_path,
            src_files={
                "__init__.py": "",
                "mod.py": ("class Foo:\n    @property\n    def wired_prop(self):\n        return 1\n\n\ndef caller(f):\n    return f.wired_prop\n"),
            },
            test_files={
                "test_x.py": ("from pkg.mod import Foo\n\n\ndef test_it():\n    f = Foo()\n    assert f.wired_prop == 1\n"),
            },
        )
        assert violations == []

    def test_silent_for_private_helper(self, tmp_path):
        violations = self._run(
            tmp_path,
            src_files={
                "__init__.py": "",
                "mod.py": "def _private_helper():\n    return 1\n",
            },
            test_files={
                "test_x.py": "from pkg.mod import _private_helper\n\n\ndef test_it():\n    assert _private_helper() == 1\n",
            },
        )
        assert violations == []

    def test_fires_on_method_called_only_from_tests(self, tmp_path):
        violations = self._run(
            tmp_path,
            src_files={
                "__init__.py": "",
                "mod.py": "class Foo:\n    def only_in_tests(self):\n        return 1\n",
            },
            test_files={
                "test_x.py": ("from pkg.mod import Foo\n\n\ndef test_it():\n    f = Foo()\n    assert f.only_in_tests() == 1\n"),
            },
        )
        assert len(violations) == 1
        assert "only_in_tests" in violations[0]

    def test_fires_on_example_only_call_site(self, tmp_path):
        violations = self._run(
            tmp_path,
            src_files={
                "__init__.py": "",
                "mod.py": "def example_only():\n    return 1\n",
            },
            test_files={},
            example_files={
                "run.py": "from pkg.mod import example_only\n\nexample_only()\n",
            },
        )
        assert len(violations) == 1
        assert "example_only" in violations[0]

    def test_silent_when_never_called_anywhere(self, tmp_path):
        # Dead code (uncalled everywhere) is a different smell than
        # "test-only" -- this checker should stay silent on it.
        violations = self._run(
            tmp_path,
            src_files={
                "__init__.py": "",
                "mod.py": "def truly_dead():\n    return 1\n",
            },
            test_files={
                "test_x.py": "def test_unrelated():\n    assert 1 == 1\n",
            },
        )
        assert violations == []
