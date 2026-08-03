"""Every production module has a matching test file (and vice versa).

Port of pyutilz's ``tests/test_meta/test_test_source_parity.py`` (PT-3),
adapted for llm_bench's smaller, flatter ``src/llm_bench`` layout and
made import-based rather than filename-based.

FORWARD check (``test_every_production_module_has_a_test_file``): for
every non-``__init__.py`` module under ``src/llm_bench``, at least one
test file under ``tests/{unit,integration,property}/`` must reference it.
"Reference" is resolved via a real ``from llm_bench... import X`` AST scan
that chases re-export chains through every ``__init__.py`` on the path
(e.g. ``from llm_bench import HalvingSchedule`` resolves through
``llm_bench/__init__.py`` -> ``llm_bench/halving/__init__.py`` ->
``llm_bench/halving/schedule.py``), NOT bare filename pattern-matching --
llm_bench's own subpackage ``__init__.py`` files re-export almost
everything through the top-level package (see ``llm_bench/__init__.py``),
so a filename-only check (module X.py -> test_X.py) would false-positive
on nearly every module that's actually exercised via ``from llm_bench
import Y``.

REVERSE check (``test_every_test_file_maps_to_a_real_module``): every test
file under ``tests/{unit,property}/`` must import at least one thing that
resolves to a real production module. ``tests/integration/`` and
``tests/test_meta/`` itself are structurally exempt -- integration tests
are inherently cross-cutting multi-module workflows (and per this repo's
own README/conftest convention some exercise the CLI/subprocess surface
without necessarily naming a specific module), and test_meta/ tests
infrastructure, not production modules.

Modules split out of an oversized file behind a pure re-export shim (this
repo's module-size-limit convention -- see ``tests/test_meta/
test_loc_budget.py`` and its 1000-LOC ceiling) don't need their own test
file: the split-out submodule is exercised through whatever already tests
the facade. Right now every such shim in this repo IS an ``__init__.py``
(``cost/__init__.py``, ``halving/__init__.py``, ``ranking/__init__.py``,
``runner/__init__.py``, ``storage/__init__.py``, ...), so excluding
``__init__.py`` outright already covers it -- but ``_is_pure_reexport_shim``
also recognises a future NON-``__init__.py`` shim (a module whose entire
body is import statements plus an optional ``__all__``) so a later
sibling-split doesn't need a manual exemption added here.

Both directions carry real false-positive risk (a module could be
exercised only via ``importlib.import_module()`` / a string-dispatched
factory that this static AST resolver can't see, e.g. ``cli/main.py``'s
own ``_load_factory`` pattern). A related blind spot: the AST scan does
not distinguish an import that is reachable at runtime from one nested
inside ``if TYPE_CHECKING:`` -- a test file whose only reference to a
module is a type-hint-only import under that guard will be treated as
"covering" it even though nothing is actually imported or exercised.
Both directions use the baseline-JSON-ratchet
pattern (see ``test_code_audit_baseline.py`` / ``test_duplicate_magic_literals.py``)
rather than a hard assert-empty: only a NEW finding fails the test.
Refresh after a deliberate, reviewed change:

    pytest tests/test_meta/test_test_source_parity.py --refresh-test-source-parity-baseline
    pytest tests/test_meta/test_test_source_parity.py --refresh-test-source-parity-reverse-baseline
"""

from __future__ import annotations

import ast
import json
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src" / "llm_bench"
_TESTS_ROOT = _REPO_ROOT / "tests"
_PACKAGE_NAME = "llm_bench"

# Directories whose test files count as evidence a module is tested
# (forward check). integration/ is included here even though it's
# reverse-exempt below -- an integration test importing a module is
# still real evidence that module is exercised, it's just not REQUIRED
# to (every integration test may instead be a pure workflow/CLI test).
_FORWARD_SCAN_DIRS: tuple[str, ...] = ("unit", "integration", "property")
# Directories whose test files must each resolve to a real module
# (reverse check). integration/ and test_meta/ are cross-cutting-exempt.
_REVERSE_SCAN_DIRS: tuple[str, ...] = ("unit", "property")

_FORWARD_BASELINE_PATH = Path(__file__).resolve().parent / "_test_source_parity_baseline.json"
_REVERSE_BASELINE_PATH = Path(__file__).resolve().parent / "_test_source_parity_reverse_baseline.json"
_FORWARD_REFRESH_FLAG = "--refresh-test-source-parity-baseline"
_REVERSE_REFRESH_FLAG = "--refresh-test-source-parity-reverse-baseline"


# ---------------------------------------------------------------------------
# AST plumbing
# ---------------------------------------------------------------------------


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None


def _package_dotted(dir_path: Path, src_root: Path, package_name: str) -> str:
    """Dotted module path of a PACKAGE directory (the thing an
    ``__init__.py`` inside it belongs to) -- e.g. ``src_root/halving``
    -> ``"llm_bench.halving"``, ``src_root`` itself -> ``"llm_bench"``.
    """
    if dir_path == src_root:
        return package_name
    rel = dir_path.relative_to(src_root)
    return ".".join((package_name, *rel.parts))


def _module_dotted(file_path: Path, src_root: Path, package_name: str) -> str:
    """Dotted module path of a ``.py`` file -- e.g.
    ``src_root/halving/schedule.py`` -> ``"llm_bench.halving.schedule"``.
    """
    rel = file_path.relative_to(src_root)
    parts = list(rel.parts)
    parts[-1] = parts[-1][:-3]  # strip ".py"
    return ".".join((package_name, *parts))


def _is_pure_reexport_shim(path: Path) -> bool:
    """True when a module's entire body is re-export plumbing: only
    ``import`` / ``from ... import`` statements, an optional module
    docstring, and an optional ``__all__`` assignment. See module
    docstring for why this matters (future sibling-split shims).
    """
    tree = _parse(path)
    if tree is None:
        return False
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]  # skip module docstring
    if not body:
        return False  # docstring-only (or fully empty) module -- not a shim, just untested
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        is_all_assign = isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
        is_all_annassign = isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "__all__"
        if is_all_assign or is_all_annassign:
            continue
        return False
    return True


def _production_modules(src_root: Path) -> list[Path]:
    """Every ``.py`` under ``src_root`` that needs its own test file:
    excludes ``__pycache__``, ``__init__.py`` / ``__main__.py``, and any
    pure re-export shim (see ``_is_pure_reexport_shim``)."""
    out: list[Path] = []
    for py in src_root.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        if py.name in {"__init__.py", "__main__.py"}:
            continue
        if _is_pure_reexport_shim(py):
            continue
        out.append(py)
    return sorted(out)


def _reexport_alias_map(src_root: Path, package_name: str) -> dict[tuple[str, str], tuple[str, str]]:
    """``{(defining_pkg_dotted, local_name): (source_module, source_name)}``
    harvested from every ``__init__.py``'s ``from <package>... import ...``
    statements. Used to chase a name imported off a subpackage (or the
    top-level package) down to the leaf module that actually defines it.
    """
    alias_map: dict[tuple[str, str], tuple[str, str]] = {}
    for init_py in src_root.rglob("__init__.py"):
        if "__pycache__" in init_py.parts:
            continue
        pkg_dotted = _package_dotted(init_py.parent, src_root, package_name)
        tree = _parse(init_py)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level != 0 or not node.module:
                continue
            if node.module != package_name and not node.module.startswith(package_name + "."):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                local_name = alias.asname or alias.name
                alias_map[(pkg_dotted, local_name)] = (node.module, alias.name)
    return alias_map


def _resolve(
    module: str,
    name: str,
    alias_map: dict[tuple[str, str], tuple[str, str]],
    prod_set: set[str],
    depth: int = 0,
    seen: set[tuple[str, str]] | None = None,
) -> str:
    """Chase ``from <module> import <name>`` down through re-export
    chains to the deepest module that actually defines/contains it.

    Two terminal cases: ``name`` is itself a submodule of ``module``
    (``from llm_bench.halving import pairing`` -> ``llm_bench.halving.pairing``,
    checked against ``prod_set`` directly), or ``name`` is a symbol with
    no further alias-map entry (defined directly in ``module``). Cycle
    guard (``seen`` / ``depth``) protects against a pathological
    self-referential ``__init__.py`` -- not expected in practice, but
    this walks real repo state, not a hand-verified fixture.
    """
    if seen is None:
        seen = set()
    if depth > 20 or (module, name) in seen:
        return module
    seen.add((module, name))
    candidate_submodule = f"{module}.{name}"
    if candidate_submodule in prod_set:
        return candidate_submodule
    if (module, name) in alias_map:
        target_module, target_name = alias_map[(module, name)]
        return _resolve(target_module, target_name, alias_map, prod_set, depth + 1, seen)
    return module


def _referenced_modules(
    path: Path,
    alias_map: dict[tuple[str, str], tuple[str, str]],
    prod_set: set[str],
    package_name: str,
) -> set[str]:
    """Every dotted module path this file's ``llm_bench``-rooted imports
    resolve to (after chasing re-export chains). NOT pre-filtered to
    ``prod_set`` -- callers intersect with ``prod_set`` themselves, since
    a resolved path that isn't a real production module (e.g. a typing
    re-export, or a name this static resolver couldn't place) is simply
    not useful signal, not an error.
    """
    tree = _parse(path)
    if tree is None:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level != 0 or not node.module:
                continue
            if node.module != package_name and not node.module.startswith(package_name + "."):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                out.add(_resolve(node.module, alias.name, alias_map, prod_set))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == package_name or alias.name.startswith(package_name + "."):
                    out.add(alias.name)
    return out


def _iter_test_files(tests_root: Path, scan_dirs: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for d in scan_dirs:
        scan_root = tests_root / d
        if not scan_root.exists():
            continue
        out.extend(p for p in scan_root.rglob("test_*.py") if "__pycache__" not in p.parts)
    return sorted(out)


# ---------------------------------------------------------------------------
# Forward / reverse scans
# ---------------------------------------------------------------------------


def _forward_untested_modules(src_root: Path, tests_root: Path, scan_dirs: Iterable[str], package_name: str) -> list[str]:
    prod_modules = _production_modules(src_root)
    prod_set = {_module_dotted(p, src_root, package_name) for p in prod_modules}
    alias_map = _reexport_alias_map(src_root, package_name)

    covered: set[str] = set()
    for test_file in _iter_test_files(tests_root, scan_dirs):
        covered |= _referenced_modules(test_file, alias_map, prod_set, package_name)
    covered &= prod_set

    return sorted(prod_set - covered)


def _reverse_orphan_test_files(src_root: Path, tests_root: Path, scan_dirs: Iterable[str], package_name: str) -> list[str]:
    prod_modules = _production_modules(src_root)
    prod_set = {_module_dotted(p, src_root, package_name) for p in prod_modules}
    alias_map = _reexport_alias_map(src_root, package_name)

    orphans: list[str] = []
    for test_file in _iter_test_files(tests_root, scan_dirs):
        referenced = _referenced_modules(test_file, alias_map, prod_set, package_name)
        if referenced & prod_set:
            continue
        orphans.append(str(test_file.relative_to(tests_root)).replace("\\", "/"))
    return sorted(orphans)


# ---------------------------------------------------------------------------
# Baseline-JSON-ratchet plumbing (mirrors test_duplicate_magic_literals.py /
# test_unwired_capabilities.py's local convention -- plain json, not orjson)
# ---------------------------------------------------------------------------


def _check_against_baseline(current: list[str], baseline_path: Path, refresh_flag: str, description: str) -> None:
    current_set = set(current)

    if refresh_flag in sys.argv or not baseline_path.exists():
        baseline_path.write_text(json.dumps(sorted(current_set), indent=2), encoding="utf-8")
        pytest.skip(f"{description} baseline refreshed at {baseline_path.name} ({len(current_set)} existing entr{'y' if len(current_set) == 1 else 'ies'}). Re-run without the refresh flag to verify.")
        return

    baseline = set(json.loads(baseline_path.read_text(encoding="utf-8")))
    new = sorted(current_set - baseline)
    if new:
        msg = "\n  ".join(new)
        pytest.fail(
            f"{len(new)} new {description} finding(s):\n  {msg}\n"
            f"Fix it (add/adjust a test), OR if confirmed legitimate, refresh the "
            f"baseline after review: pytest tests/test_meta/test_test_source_parity.py {refresh_flag}"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_production_module_has_a_test_file():
    untested = _forward_untested_modules(_SRC_ROOT, _TESTS_ROOT, _FORWARD_SCAN_DIRS, _PACKAGE_NAME)
    _check_against_baseline(
        untested,
        _FORWARD_BASELINE_PATH,
        _FORWARD_REFRESH_FLAG,
        f"untested-production-module (no test under tests/{{{','.join(_FORWARD_SCAN_DIRS)}}}/ imports it, directly or via a resolved re-export chain)",
    )


def test_every_test_file_maps_to_a_real_module():
    orphans = _reverse_orphan_test_files(_SRC_ROOT, _TESTS_ROOT, _REVERSE_SCAN_DIRS, _PACKAGE_NAME)
    _check_against_baseline(
        orphans,
        _REVERSE_BASELINE_PATH,
        _REVERSE_REFRESH_FLAG,
        f"orphan-test-file (no resolved llm_bench import lands on a real src/llm_bench module; checked under tests/{{{','.join(_REVERSE_SCAN_DIRS)}}}/)",
    )


# ---------------------------------------------------------------------------
# Direct regression tests for the checker itself, via synthetic src/ +
# tests/ trees under tmp_path (mirrors test_unwired_capabilities.py's
# TestScanUnwiredCapabilitiesDetectsShapes convention).
# ---------------------------------------------------------------------------


class TestParityCheckerDetectsShapes:
    def _make_tree(self, tmp_path: Path, *, src_files: dict[str, str], test_files: dict[str, str]) -> tuple[Path, Path]:
        src_root = tmp_path / "src" / "pkg"
        tests_root = tmp_path / "tests"
        for rel, content in src_files.items():
            p = src_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        for rel, content in test_files.items():
            p = tests_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return src_root, tests_root

    def test_fires_on_module_with_no_referencing_test(self, tmp_path):
        src_root, tests_root = self._make_tree(
            tmp_path,
            src_files={
                "__init__.py": "",
                "orphan.py": "def helper():\n    return 1\n",
            },
            test_files={
                "unit/test_unrelated.py": "def test_it():\n    assert 1 == 1\n",
            },
        )
        untested = _forward_untested_modules(src_root, tests_root, ("unit",), "pkg")
        assert untested == ["pkg.orphan"]

    def test_silent_on_direct_import(self, tmp_path):
        src_root, tests_root = self._make_tree(
            tmp_path,
            src_files={
                "__init__.py": "",
                "mod.py": "def helper():\n    return 1\n",
            },
            test_files={
                "unit/test_mod.py": "from pkg.mod import helper\n\n\ndef test_it():\n    assert helper() == 1\n",
            },
        )
        untested = _forward_untested_modules(src_root, tests_root, ("unit",), "pkg")
        assert untested == []

    def test_silent_when_covered_via_two_level_reexport_chain(self, tmp_path):
        # from pkg import Thing -> pkg/__init__.py re-exports from pkg.sub
        # -> pkg/sub/__init__.py re-exports from pkg.sub.deep -- proves the
        # resolver chases re-exports rather than requiring a direct import,
        # exactly the shape llm_bench's own top-level __init__.py uses.
        src_root, tests_root = self._make_tree(
            tmp_path,
            src_files={
                "__init__.py": "from pkg.sub import Thing\n",
                "sub/__init__.py": "from pkg.sub.deep import Thing\n",
                "sub/deep.py": "class Thing:\n    pass\n",
            },
            test_files={
                "unit/test_thing.py": "from pkg import Thing\n\n\ndef test_it():\n    assert Thing is not None\n",
            },
        )
        untested = _forward_untested_modules(src_root, tests_root, ("unit",), "pkg")
        assert untested == []

    def test_pure_reexport_shim_needs_no_test_file(self, tmp_path):
        # A non-__init__.py module whose whole body is import plumbing
        # (this repo's module-size-limit split convention) is excluded
        # from the production-module set outright, whether or not
        # anything actually imports from it.
        src_root, tests_root = self._make_tree(
            tmp_path,
            src_files={
                "__init__.py": "",
                "real.py": "def helper():\n    return 1\n",
                "shim.py": '"""Re-export shim after a module-size split."""\n\nfrom pkg.real import helper\n\n__all__ = ["helper"]\n',
            },
            test_files={
                "unit/test_real.py": "from pkg.real import helper\n\n\ndef test_it():\n    assert helper() == 1\n",
            },
        )
        untested = _forward_untested_modules(src_root, tests_root, ("unit",), "pkg")
        assert untested == []

    def test_module_body_module_still_flagged(self, tmp_path):
        # A module that has real logic (not just imports) alongside an
        # import must NOT be misclassified as a re-export shim.
        src_root, tests_root = self._make_tree(
            tmp_path,
            src_files={
                "__init__.py": "",
                "mixed.py": "from pkg.other import helper\n\n\ndef real_logic():\n    return helper() + 1\n",
                "other.py": "def helper():\n    return 1\n",
            },
            test_files={
                "unit/test_other.py": "from pkg.other import helper\n\n\ndef test_it():\n    assert helper() == 1\n",
            },
        )
        untested = _forward_untested_modules(src_root, tests_root, ("unit",), "pkg")
        assert untested == ["pkg.mixed"]

    def test_reverse_fires_on_test_file_with_no_package_import(self, tmp_path):
        src_root, tests_root = self._make_tree(
            tmp_path,
            src_files={"__init__.py": "", "mod.py": "def helper():\n    return 1\n"},
            test_files={"unit/test_orphan.py": "def test_it():\n    assert 1 == 1\n"},
        )
        orphans = _reverse_orphan_test_files(src_root, tests_root, ("unit",), "pkg")
        assert orphans == ["unit/test_orphan.py"]

    def test_reverse_silent_when_test_resolves_to_real_module(self, tmp_path):
        src_root, tests_root = self._make_tree(
            tmp_path,
            src_files={"__init__.py": "", "mod.py": "def helper():\n    return 1\n"},
            test_files={"unit/test_mod.py": "from pkg.mod import helper\n\n\ndef test_it():\n    assert helper() == 1\n"},
        )
        orphans = _reverse_orphan_test_files(src_root, tests_root, ("unit",), "pkg")
        assert orphans == []
