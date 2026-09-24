"""No module is reloaded or dropped from ``sys.modules`` without a restore in the same scope.

Reloading a module rebinds its top-level names: a test file that did ``from llm_bench.X import Y`` at load keeps the OLD
``Y`` while a lazy import gets the NEW one, and ``isinstance`` checks, class-attribute caches and idempotent install
markers break in unrelated later tests. The check is ``py_ci_shared.module_reload_safety``: in tests each
``importlib.reload`` / ``del sys.modules[...]`` / ``sys.modules.pop(...)`` needs a snapshot restore reachable from its
own function or fixture; in ``src/`` any use fails. It is stricter than the local copy it replaces: a
``subprocess.run`` next to an in-process reload no longer counts as isolation, since the reload still ran in-process.
"""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.module_reload_safety import assert_no_reloads_in_code, assert_no_unpaired_reloads

TESTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_DIR.parent

# Files that use the primitives only on stub modules of their own, relative to tests/.
_STUB_ONLY_FILES = (
    # sys.modules.pop(mod_name, None) in a finally removes a synthetic shim module written to tmp_path and imported only
    # inside that test; it was never in sys.modules before, so there is no prior binding to restore.
    "test_meta/test_entry_points_resolvable.py",
)

# llm_bench modules that own module-level mutable singletons; none today.
_SINGLETON_MODULES: tuple[str, ...] = ()


def test_no_unpaired_module_reload_in_tests():
    assert_no_unpaired_reloads(TESTS_DIR, stub_only_files=_STUB_ONLY_FILES, singleton_modules=_SINGLETON_MODULES)


def test_no_module_reload_in_production_code():
    assert_no_reloads_in_code([REPO_ROOT / "src" / "llm_bench"], REPO_ROOT)
