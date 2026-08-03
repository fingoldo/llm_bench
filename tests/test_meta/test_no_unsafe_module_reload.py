"""Meta-test: every ``importlib.reload(...)`` / ``del sys.modules[...]`` /
``sys.modules.pop(...)`` in tests/ must be paired with a snapshot+restore
mechanism IN THE SAME SCOPE so module rebinding does not split class
identity for sibling tests.

Background (repo owner's standing rule, ported from mlframe's
test_no_unsafe_module_reload.py, see mlframe CLAUDE.md "Test pollution:
never rebind a module without snapshot/restore"): reloading a module
rebinds its top-level symbols. Any test file that did
``from llm_bench.X import Y`` at file-load keeps a reference to the OLD
Y; lazy ``from .X import Y`` inside function bodies resolves to the NEW
Y. The two ends disagree on class identity, breaking isinstance checks,
class-attribute caches, and idempotent install markers in unrelated
LATER tests (the pollution only surfaces once another test runs after
the reload, so it is easy to miss in isolation).

Sensor logic (AST-scoped, not whole-file string match):
- AST-scan every test file under tests/ for the three primitives.
- For each hit, locate its ENCLOSING function/fixture and require a
  paired restore mechanism reachable from that same scope:
  a) a restore in the enclosing function's own body
     (``finally``/teardown restoring ``sys.modules`` or a module
     ``__dict__``, or an ``addfinalizer(restore_callable)``), OR
  b) the enclosing function requests (by parameter name) a fixture
     defined in the same file whose body performs the restore, OR
  c) an autouse fixture in the same file performs the restore, OR
  d) a ``subprocess.run(...)`` in the enclosing function (full
     process isolation -- no in-process module mutation to restore).

Whole-file string matching would be too coarse: a file could carry a
restore marker in an unrelated function and pass even though the
reload site itself was unpaired. The AST scoping closes that.

Additionally, reloads/deletes of llm_bench modules that own
module-level mutable singletons (caches/registries/locks/connection
pools) require subprocess isolation specifically -- a ``__dict__`` or
``sys.modules`` swap is NOT accepted as a restore for these, since it
does not rebuild a singleton object that other modules already
captured by reference before the reload.

As of writing, llm_bench's test suite does no module reloading at all
(grep confirmed zero ``importlib.reload`` / ``del sys.modules[...]``
hits under tests/) -- this seeds a purely preventative regression
guard for the day someone reaches for one to "force a fresh import".
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent.parent

# Files known to use the primitives ONLY against non-llm_bench stub/mock
# modules (e.g. a fake provider module injected for CLI plugin-discovery
# tests) where rebinding does not split llm_bench class identity. Listed
# by repo-relative posix path. Empty today -- add an entry only with a
# one-line rationale for why the reload target can't split real state.
_KNOWN_STUB_ONLY_FILES: frozenset[str] = frozenset(
    {
        # test_entry_points_resolvable.py: sys.modules.pop(mod_name, None) in a finally
        # block removes a synthetic shim module written to a tmp_path dir and imported
        # ONLY inside that one test via importlib.import_module(module_name) -- it was
        # never present in sys.modules before the test ran, so there is no prior binding
        # to restore. This is cleanup of injected test-only state, not a rebind of a real
        # module that other code could have captured a stale reference to.
        "test_meta/test_entry_points_resolvable.py",
    }
)

# llm_bench modules that own module-level mutable singletons (caches /
# registries / locks / connection pools). Reloading these splits the
# singleton even with a __dict__ snapshot+restore, because importers
# captured the OLD singleton object by reference. Used to escalate the
# failure message. Empty today (no such module identified in
# src/llm_bench) -- keep the plumbing so a future singleton-owning
# module (e.g. a process-wide connection pool) gets flagged for real.
_SINGLETON_OWNING_MODULES: frozenset[str] = frozenset()

_RELOAD_PRIMITIVES = ("importlib.reload", "del sys.modules", "sys.modules.pop")


def _is_reload_call(node: ast.Call | ast.Delete) -> str | None:
    """Return the primitive name if ``node`` is a reload primitive call/delete, else None."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "reload" and isinstance(func.value, ast.Name) and func.value.id == "importlib":
            return "importlib.reload"
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "pop"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "modules"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
        ):
            return "sys.modules.pop"
    if isinstance(node, ast.Delete):
        for tgt in node.targets:
            if (
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.value, ast.Attribute)
                and tgt.value.attr == "modules"
                and isinstance(tgt.value.value, ast.Name)
                and tgt.value.value.id == "sys"
            ):
                return "del sys.modules"
    return None


def _import_alias_map(tree: ast.AST) -> dict[str, str]:
    """Map every local name bound by ``import x.y as z`` / ``import x.y`` to its full
    dotted module path, so a reload call site's argument (which names the LOCAL alias,
    not the dotted path) can be resolved back to the module it actually targets."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname if alias.asname else alias.name.split(".")[0]
                aliases[local] = alias.name
    return aliases


def _reload_target_module_name(node: ast.Call | ast.Delete, alias_map: dict[str, str]) -> str | None:
    """Resolve the dotted module name a reload/delete SITE actually targets, else None."""
    if isinstance(node, ast.Call):
        if node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Name) and arg.id in alias_map:
                return alias_map[arg.id]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
        return None
    for tgt in node.targets:
        if isinstance(tgt, ast.Subscript):
            key = tgt.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                return key.value
    return None


def _reload_targets_singleton_module(node: ast.Call | ast.Delete, alias_map: dict[str, str]) -> bool:
    """True if THIS specific reload/delete SITE names a known singleton-owning llm_bench
    module (resolved through import aliases), not merely if the module is mentioned
    somewhere else in the file.

    Scoped to the individual call site rather than the whole file: a file can legitimately
    touch a singleton-owning module in one test and an ordinary module in another, and the
    escalation to "subprocess isolation required" must apply only to the site that actually
    reloads the singleton-owning module, not to every reload in the file."""
    target = _reload_target_module_name(node, alias_map)
    if target is None:
        return False
    return any(mod == target or target.startswith(mod + ".") for mod in _SINGLETON_OWNING_MODULES)


def _node_has_subprocess_isolation(node: ast.AST) -> bool:
    """True if ``node`` contains a ``subprocess.run(...)`` call anywhere in its subtree.

    Full process isolation -- there is no in-process module mutation left to restore,
    so this is the only mechanism accepted for reloads of singleton-owning modules."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr == "run" and isinstance(f.value, ast.Name) and f.value.id == "subprocess":
                return True
    return False


def _node_has_restore(node: ast.AST) -> bool:
    """Detect a restore mechanism anywhere within ``node`` (a function/fixture body subtree).

    Accepted: ``sys.modules[...] = <name>`` re-assignment (snapshot restore), ``sys.modules.update(...)``,
    a module ``__dict__.clear()`` / ``__dict__.update(...)`` pair, ``addfinalizer(<callable>)``, or
    ``subprocess.run(...)`` (process isolation)."""
    if _node_has_subprocess_isolation(node):
        return True
    for sub in ast.walk(node):
        # sys.modules[...] = ...  (restore from a saved reference)
        if isinstance(sub, ast.Assign):
            for tgt in sub.targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Attribute)
                    and tgt.value.attr == "modules"
                    and isinstance(tgt.value.value, ast.Name)
                    and tgt.value.value.id == "sys"
                ):
                    return True
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute):
                # sys.modules.update(...)
                if (
                    f.attr == "update"
                    and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "modules"
                    and isinstance(f.value.value, ast.Name)
                    and f.value.value.id == "sys"
                ):
                    return True
                # <mod>.__dict__.update(...) / .clear()  -- module __dict__ swap
                if f.attr in ("update", "clear") and isinstance(f.value, ast.Attribute) and f.value.attr == "__dict__":
                    return True
                # request.addfinalizer(<restore>)
                if f.attr == "addfinalizer":
                    return True
    return False


def _iter_func_defs(tree: ast.AST):
    """Yield every FunctionDef / AsyncFunctionDef node in ``tree``."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _is_fixture(func: ast.AST) -> tuple[bool, bool]:
    """Return (is_fixture, is_autouse) for a function def by inspecting its decorators."""
    is_fixture = False
    is_autouse = False
    for dec in getattr(func, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            is_fixture = True
        elif isinstance(target, ast.Name) and target.id == "fixture":
            is_fixture = True
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "autouse" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    is_autouse = True
    return is_fixture, is_autouse


def _enclosing_func(tree: ast.AST, target: ast.Call | ast.Delete) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the innermost FunctionDef enclosing ``target`` (by line span), else None."""
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for func in _iter_func_defs(tree):
        start = func.lineno
        end = getattr(func, "end_lineno", start)
        if start <= target.lineno <= end:
            if best is None or func.lineno > best.lineno:
                best = func
    return best


def _fixture_restore_map(tree: ast.AST, restore_check: Callable[[ast.AST], bool]) -> tuple[dict[str, bool], bool]:
    """Return (fixture_name -> has_restore, any_autouse_restore_fixture) for fixtures defined in ``tree``."""
    fixture_restore: dict[str, bool] = {}
    autouse_restore = False
    for func in _iter_func_defs(tree):
        is_fixture, is_autouse = _is_fixture(func)
        if not is_fixture:
            continue
        has = restore_check(func)
        fixture_restore[func.name] = has
        if is_autouse and has:
            autouse_restore = True
    return fixture_restore, autouse_restore


def _is_reload_site_safe(
    node: ast.Call | ast.Delete,
    tree: ast.AST,
    restore_check: Callable[[ast.AST], bool],
    fixture_restore: dict[str, bool],
    autouse_restore: bool,
) -> bool:
    """True if ``node`` (a reload primitive site) has a paired restore reachable from its own scope."""
    if autouse_restore:
        return True
    enclosing = _enclosing_func(tree, node)
    if enclosing is None:
        return False
    if restore_check(enclosing):
        return True
    # The enclosing function may delegate the restore to a requested fixture.
    arg_names = {a.arg for a in enclosing.args.args}
    return any(fixture_restore.get(name, False) for name in arg_names)


def _collect_unsafe_sites() -> list[tuple[str, int, str, bool]]:
    """Return (rel_path, lineno, primitive, hits_singleton) tuples for AST-scoped-unsafe sites."""
    out: list[tuple[str, int, str, bool]] = []
    for py in TESTS_DIR.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            src = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not any(tok in src for tok in _RELOAD_PRIMITIVES):
            continue
        rel = py.relative_to(TESTS_DIR).as_posix()
        if rel in _KNOWN_STUB_ONLY_FILES:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        # Precompute BOTH fixture-restore maps up front: which check applies (normal
        # restore vs. subprocess-isolation-only) is decided per reload SITE below,
        # since a single file can mix ordinary and singleton-owning reload targets.
        normal_fixture_restore, normal_autouse_restore = _fixture_restore_map(tree, _node_has_restore)
        strict_fixture_restore, strict_autouse_restore = _fixture_restore_map(tree, _node_has_subprocess_isolation)
        alias_map = _import_alias_map(tree)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Call, ast.Delete)):
                continue
            prim = _is_reload_call(node)
            if prim is None:
                continue
            hits_singleton = _reload_targets_singleton_module(node, alias_map)
            # Singleton-owning modules accept ONLY subprocess isolation as a restore --
            # a __dict__/sys.modules swap does not rebuild a singleton object other
            # modules already captured by reference before the reload.
            if hits_singleton:
                restore_check = _node_has_subprocess_isolation
                fixture_restore, autouse_restore = strict_fixture_restore, strict_autouse_restore
            else:
                restore_check = _node_has_restore
                fixture_restore, autouse_restore = normal_fixture_restore, normal_autouse_restore
            if not _is_reload_site_safe(node, tree, restore_check, fixture_restore, autouse_restore):
                out.append((rel, node.lineno, prim, hits_singleton))
    return out


def test_no_unpaired_module_reload_in_tests():
    """No unpaired module reload in tests/ -- pair every reload/del site with a snapshot+restore."""
    sites = _collect_unsafe_sites()
    if sites:
        formatted = "\n  ".join(f"{p}:{ln} ({prim})" + ("  [!! reloads a singleton-owning llm_bench module]" if sing else "") for p, ln, prim, sing in sites[:30])
        more = f"\n  ... and {len(sites) - 30} more" if len(sites) > 30 else ""
        pytest.fail(
            f"{len(sites)} test reload site(s) lack a snapshot+restore mechanism reachable from the "
            f"SAME function/fixture scope. Per the repo's 'never rebind a module without "
            f"snapshot/restore' rule this is a cross-test pollution risk: restore in the same "
            f"function (finally / addfinalizer), a requested fixture, an autouse fixture, or isolate "
            f"the reload in a subprocess. Reloads of singleton-owning modules need subprocess "
            f"isolation (a __dict__ swap does not rebuild the captured singleton).\n  " + formatted + more
        )


def test_known_stub_only_files_actually_exist():
    """Guard against the allowlist going stale (file renames / removals)."""
    for rel in _KNOWN_STUB_ONLY_FILES:
        assert (
            TESTS_DIR / rel
        ).exists(), f"_KNOWN_STUB_ONLY_FILES entry {rel!r} no longer exists. Remove it from tests/test_meta/test_no_unsafe_module_reload.py"


class TestCollectUnsafeSitesDetectsShapes:
    """Direct regression tests for the checker itself, via synthetic source
    written to a real temp file under ``tmp_path`` (the scanner reads
    from disk via ``TESTS_DIR.rglob``, so a monkeypatch of ``TESTS_DIR``
    is used to point the scan at the synthetic tree instead of the real
    llm_bench tests/ directory)."""

    def _run(self, tmp_path, monkeypatch, source: str) -> list[tuple[str, int, str, bool]]:
        synthetic_dir = tmp_path / "tests"
        synthetic_dir.mkdir()
        (synthetic_dir / "test_synthetic.py").write_text(source, encoding="utf-8")
        monkeypatch.setattr(sys.modules[__name__], "TESTS_DIR", synthetic_dir)
        return _collect_unsafe_sites()

    def test_bare_importlib_reload_flagged(self, tmp_path, monkeypatch):
        source = "import importlib\n" "import llm_bench.core.types as t\n" "\n" "def test_x():\n" "    importlib.reload(t)\n"
        sites = self._run(tmp_path, monkeypatch, source)
        assert len(sites) == 1
        assert sites[0][2] == "importlib.reload"

    def test_bare_del_sys_modules_flagged(self, tmp_path, monkeypatch):
        source = "import sys\n" "\n" "def test_x():\n" "    del sys.modules['llm_bench.core.types']\n"
        sites = self._run(tmp_path, monkeypatch, source)
        assert len(sites) == 1
        assert sites[0][2] == "del sys.modules"

    def test_bare_sys_modules_pop_flagged(self, tmp_path, monkeypatch):
        source = "import sys\n" "\n" "def test_x():\n" "    sys.modules.pop('llm_bench.core.types')\n"
        sites = self._run(tmp_path, monkeypatch, source)
        assert len(sites) == 1
        assert sites[0][2] == "sys.modules.pop"

    def test_reload_with_same_scope_snapshot_restore_not_flagged(self, tmp_path, monkeypatch):
        source = (
            "import sys\n"
            "import importlib\n"
            "import llm_bench.core.types as t\n"
            "\n"
            "def test_x():\n"
            "    saved = sys.modules['llm_bench.core.types']\n"
            "    try:\n"
            "        importlib.reload(t)\n"
            "    finally:\n"
            "        sys.modules['llm_bench.core.types'] = saved\n"
        )
        sites = self._run(tmp_path, monkeypatch, source)
        assert sites == []

    def test_reload_restored_via_requested_fixture_not_flagged(self, tmp_path, monkeypatch):
        source = (
            "import sys\n"
            "import importlib\n"
            "import pytest\n"
            "import llm_bench.core.types as t\n"
            "\n"
            "@pytest.fixture\n"
            "def restore_types():\n"
            "    saved = sys.modules['llm_bench.core.types']\n"
            "    yield\n"
            "    sys.modules['llm_bench.core.types'] = saved\n"
            "\n"
            "def test_x(restore_types):\n"
            "    importlib.reload(t)\n"
        )
        sites = self._run(tmp_path, monkeypatch, source)
        assert sites == []

    def test_reload_restored_via_autouse_fixture_not_flagged(self, tmp_path, monkeypatch):
        source = (
            "import sys\n"
            "import importlib\n"
            "import pytest\n"
            "import llm_bench.core.types as t\n"
            "\n"
            "@pytest.fixture(autouse=True)\n"
            "def _restore_types():\n"
            "    saved = sys.modules['llm_bench.core.types']\n"
            "    yield\n"
            "    sys.modules['llm_bench.core.types'] = saved\n"
            "\n"
            "def test_x():\n"
            "    importlib.reload(t)\n"
        )
        sites = self._run(tmp_path, monkeypatch, source)
        assert sites == []

    def test_reload_isolated_in_subprocess_not_flagged(self, tmp_path, monkeypatch):
        source = "import subprocess\n" "import importlib\n" "\n" "def test_x():\n" "    subprocess.run(['python', '-c', 'import importlib; importlib.reload(importlib)'])\n"
        sites = self._run(tmp_path, monkeypatch, source)
        assert sites == []

    def test_restore_in_unrelated_function_still_flagged(self, tmp_path, monkeypatch):
        # Whole-file string matching was the prior (too coarse) heuristic: a restore
        # marker elsewhere in the file must NOT launder an unpaired reload site.
        source = (
            "import sys\n"
            "import importlib\n"
            "import llm_bench.core.types as t\n"
            "\n"
            "def test_unpaired():\n"
            "    importlib.reload(t)\n"
            "\n"
            "def test_unrelated():\n"
            "    saved = sys.modules['llm_bench.core.types']\n"
            "    sys.modules['llm_bench.core.types'] = saved\n"
        )
        sites = self._run(tmp_path, monkeypatch, source)
        assert len(sites) == 1

    def test_reload_of_singleton_owning_module_dict_restore_still_flagged(self, tmp_path, monkeypatch):
        # A __dict__ swap is accepted as a restore for ordinary modules, but a
        # singleton-owning module (registered in _SINGLETON_OWNING_MODULES) requires
        # subprocess isolation specifically -- a __dict__ swap does not rebuild a
        # singleton object other modules already captured by reference.
        monkeypatch.setattr(sys.modules[__name__], "_SINGLETON_OWNING_MODULES", frozenset({"llm_bench.pool.base"}))
        source = (
            "import importlib\n"
            "import llm_bench.pool.base as pool_base\n"
            "\n"
            "def test_x():\n"
            "    saved = dict(pool_base.__dict__)\n"
            "    try:\n"
            "        importlib.reload(pool_base)\n"
            "    finally:\n"
            "        pool_base.__dict__.clear()\n"
            "        pool_base.__dict__.update(saved)\n"
        )
        sites = self._run(tmp_path, monkeypatch, source)
        assert len(sites) == 1
        assert sites[0][3] is True  # hits_singleton

    def test_reload_of_singleton_owning_module_subprocess_isolation_not_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys.modules[__name__], "_SINGLETON_OWNING_MODULES", frozenset({"llm_bench.pool.base"}))
        source = (
            "import subprocess\n"
            "import importlib\n"
            "import llm_bench.pool.base as pool_base\n"
            "\n"
            "def test_x():\n"
            "    subprocess.run(['python', '-c', 'import importlib; importlib.reload(importlib)'])\n"
        )
        sites = self._run(tmp_path, monkeypatch, source)
        assert sites == []

    def test_singleton_reload_does_not_leak_escalation_to_ordinary_reload_in_same_file(self, tmp_path, monkeypatch):
        # Regression for a file-level (rather than call-site-level) hits_singleton bug:
        # a file that reloads a singleton-owning module (needs subprocess isolation) AND,
        # in a separate function, reloads an ordinary module with a proper same-scope
        # dict/sys.modules restore must flag ONLY the singleton site -- the ordinary,
        # correctly-paired site must not be swept up by the file-wide singleton check.
        monkeypatch.setattr(sys.modules[__name__], "_SINGLETON_OWNING_MODULES", frozenset({"llm_bench.pool.base"}))
        source = (
            "import sys\n"
            "import importlib\n"
            "import llm_bench.pool.base as pool_base\n"
            "import llm_bench.core.types as t\n"
            "\n"
            "def test_singleton_reload():\n"
            "    saved = dict(pool_base.__dict__)\n"
            "    try:\n"
            "        importlib.reload(pool_base)\n"
            "    finally:\n"
            "        pool_base.__dict__.clear()\n"
            "        pool_base.__dict__.update(saved)\n"
            "\n"
            "def test_ordinary_reload_properly_paired():\n"
            "    saved = sys.modules['llm_bench.core.types']\n"
            "    try:\n"
            "        importlib.reload(t)\n"
            "    finally:\n"
            "        sys.modules['llm_bench.core.types'] = saved\n"
        )
        sites = self._run(tmp_path, monkeypatch, source)
        assert len(sites) == 1
        assert sites[0][3] is True  # only the singleton site is flagged
        assert sites[0][1] == 9  # importlib.reload(pool_base) line

    def test_clean_file_with_no_reload_primitives_not_scanned(self, tmp_path, monkeypatch):
        source = "def test_x():\n" "    assert 1 == 1\n"
        sites = self._run(tmp_path, monkeypatch, source)
        assert sites == []
