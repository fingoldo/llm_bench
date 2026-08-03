"""AST-scan for asyncpg.create_pool() / asyncpg.connect() calls missing a
timeout kwarg.

Motivating finding: ``asyncpg.create_pool(dsn=self._url, min_size=...,
max_size=...)`` with no ``command_timeout=`` means one hung Postgres query
can occupy a pool connection indefinitely -- and since a pool has a fixed
``max_size``, enough hangs eventually starve every other caller with no
self-recovery. pyutilz's ``network_timeout.py`` scanner already flags this
call shape for ``requests``/``httpx``/``urllib``, but doesn't (yet)
recognize asyncpg pool-creation/connection calls, which take a
differently-named timeout kwarg (``command_timeout=`` for
``create_pool()``, ``timeout=`` for a bare ``connect()``).

This overlaps in intent with ``test_storage_backend_hardening.py``'s
``scan_command_timeout`` (finding 4 there), but is broader and more
precise:

  - scans every ``.py`` file under ``src/llm_bench``, not just
    ``postgres.py`` -- a second asyncpg call site added anywhere else in
    the tree is caught too;
  - resolves ``asyncpg.create_pool`` / ``asyncpg.connect`` by actually
    tracing the file's ``import`` / ``from ... import`` statements (an
    aliased ``import asyncpg as apg`` or a ``from asyncpg import
    create_pool`` both resolve correctly), not by string-matching a bare
    ``Attribute.value.id == "asyncpg"`` -- so a same-named ``create_pool``
    from an unrelated module is never falsely flagged;
  - also covers ``asyncpg.connect()`` (its own timeout kwarg is
    ``timeout=``, not ``command_timeout=``), which the other checker
    doesn't look at.

Confirmed against the current repo (2026-07-22): this codebase has
exactly one asyncpg call site -- ``PostgresStorage.initialize()`` in
``storage/postgres.py`` -- and it already passes
``command_timeout=self._command_timeout``, so this scanner runs clean
today; it exists to catch a regression or a second call site added later.
Low false-positive risk in a codebase with exactly one Postgres backend,
so this uses a hard assert-empty rather than the baseline-JSON-ratchet
pattern (see ``test_code_audit_baseline.py`` for when that pattern is
warranted instead).

Whitelist a confirmed false positive via ``_WHITELIST`` below, keyed
``"<rel_path>::<line>"``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench"

# Per-file whitelist: "<rel_path>::<line>" entries skipped.
_WHITELIST: set[str] = set()

# asyncpg attribute name -> required timeout kwarg name(s). ``connect()``
# requires BOTH: ``timeout=`` only bounds the initial connection handshake
# (and already defaults to a finite 60s), it does NOT bound query
# execution -- ``command_timeout=`` (default ``None``/unbounded) is the
# actual guard against a hung query on a standalone connection, which is
# the exact threat this scanner exists to catch.
_REQUIRED_TIMEOUT_KWARG: dict[str, tuple[str, ...]] = {
    "create_pool": ("command_timeout",),
    "connect": ("timeout", "command_timeout"),
}


def _collect_asyncpg_aliases(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    """Trace every ``import``/``from ... import`` statement reachable in
    ``tree`` (at ANY nesting depth -- ``postgres.py`` itself does a lazy
    ``import asyncpg`` inside ``initialize()``, not at module scope) and
    return ``(module_aliases, func_aliases)``:

    - ``module_aliases``: local name -> ``"asyncpg"`` for ``import
      asyncpg`` / ``import asyncpg as apg``.
    - ``func_aliases``: local name -> ``"create_pool"``/``"connect"`` for
      ``from asyncpg import create_pool`` / ``from asyncpg import connect
      as conn``.

    Binding all of a file's imports together regardless of the scope they
    appear in (rather than doing full lexical-scope resolution) is a
    deliberate simplification: this repo has a single, small Postgres
    backend module, so a name collision between an unrelated function-
    local ``asyncpg`` binding and this checker's file-wide view is not a
    realistic risk here.
    """
    module_aliases: dict[str, str] = {}
    func_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import asyncpg.pool` binds the top-level name `asyncpg`
                # exactly like `import asyncpg` does -- match any dotted
                # path rooted at `asyncpg`, not just the exact string.
                top = alias.name.split(".")[0]
                if top == "asyncpg":
                    module_aliases[alias.asname or top] = "asyncpg"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "asyncpg" or module.startswith("asyncpg."):
                for alias in node.names:
                    if alias.name in _REQUIRED_TIMEOUT_KWARG:
                        func_aliases[alias.asname or alias.name] = alias.name
    return module_aliases, func_aliases


def _resolve_asyncpg_attr(call: ast.Call, module_aliases: dict[str, str], func_aliases: dict[str, str]) -> str | None:
    """Return ``"create_pool"``/``"connect"`` if ``call`` resolves to
    ``asyncpg.create_pool``/``asyncpg.connect`` (however it was imported),
    else ``None``. Import-aware: an unrelated module's same-named
    ``create_pool``/``connect`` function is never matched, since it has
    no entry in ``module_aliases``/``func_aliases``."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _REQUIRED_TIMEOUT_KWARG:
        if isinstance(func.value, ast.Name) and module_aliases.get(func.value.id) == "asyncpg":
            return func.attr
        return None
    if isinstance(func, ast.Name):
        resolved = func_aliases.get(func.id)
        if resolved in _REQUIRED_TIMEOUT_KWARG:
            return resolved
    return None


def scan_asyncpg_timeout_kwargs(path: Path, rel: str) -> list[str]:
    """Flag every ``asyncpg.create_pool(...)``/``asyncpg.connect(...)``
    call missing its required timeout kwarg."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [f"{rel}: SyntaxError: {e}"]

    module_aliases, func_aliases = _collect_asyncpg_aliases(tree)

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _resolve_asyncpg_attr(node, module_aliases, func_aliases)
        if resolved is None:
            continue
        # A bare `**kwargs` spread can legitimately supply the timeout
        # kwarg at runtime with no literal keyword visible in the AST --
        # flagging it anyway would be a pure false positive this checker
        # cannot resolve statically, so it is skipped rather than guessed
        # at (mirrors `test_duplicate_magic_literals.py`'s treatment of
        # `**kwargs` spreads).
        if any(kw.arg is None for kw in node.keywords):
            continue
        required_kwargs = _REQUIRED_TIMEOUT_KWARG[resolved]
        # `command_timeout=None`/`timeout=None` is asyncpg's own sentinel
        # for "no timeout enforced" (see `inspect.signature(asyncpg.connect)`
        # -- both default to that exact value) -- functionally identical to
        # omitting the kwarg entirely, so a literal `None` must be treated
        # as missing, not as a satisfied requirement.
        present_non_none = {kw.arg for kw in node.keywords if kw.arg is not None and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)}
        missing = [r for r in required_kwargs if r not in present_non_none]
        if not missing:
            continue
        key = f"{rel}::{node.lineno}"
        if key in _WHITELIST:
            continue
        missing_str = ", ".join(f"{m}=" for m in missing)
        violations.append(
            f"{rel}:{node.lineno}: asyncpg.{resolved}(...) call is missing "
            f"or has a None-valued '{missing_str}' kwarg -- a hung Postgres "
            f"query/connection can occupy the pool indefinitely with no "
            f"self-recovery. Add {missing_str}<seconds>."
        )
    return violations


def test_no_missing_asyncpg_timeout_kwarg():
    all_violations: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(_SRC_ROOT))
        all_violations.extend(scan_asyncpg_timeout_kwargs(path, rel))
    if all_violations:
        msg = "\n  ".join(all_violations)
        pytest.fail(f"asyncpg call(s) missing a timeout kwarg:\n  {msg}")


class TestScanAsyncpgTimeoutKwargsDetectsShapes:
    """Direct regression tests for the checker itself, via synthetic
    source on a real temp file (the checker reads from disk, not from a
    string)."""

    def _run(self, tmp_path, source: str) -> list[str]:
        p = tmp_path / "synthetic.py"
        p.write_text(source, encoding="utf-8")
        return scan_asyncpg_timeout_kwargs(p, "synthetic.py")

    def test_create_pool_missing_command_timeout_fires(self, tmp_path):
        source = "import asyncpg\n" "async def f():\n" "    return await asyncpg.create_pool(dsn='x', min_size=1, max_size=8)\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "command_timeout" in violations[0]

    def test_create_pool_with_command_timeout_is_clean(self, tmp_path):
        source = "import asyncpg\n" "async def f():\n" "    return await asyncpg.create_pool(dsn='x', min_size=1, max_size=8, command_timeout=30.0)\n"
        assert self._run(tmp_path, source) == []

    def test_connect_missing_timeout_fires(self, tmp_path):
        source = "import asyncpg\n" "async def f():\n" "    return await asyncpg.connect(dsn='x')\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "timeout=" in violations[0]
        assert "command_timeout=" in violations[0]

    def test_connect_with_timeout_only_still_fires(self, tmp_path):
        # `timeout=` alone only bounds the connection handshake, not query
        # execution -- `command_timeout=` is also required for `connect()`.
        source = "import asyncpg\n" "async def f():\n" "    return await asyncpg.connect(dsn='x', timeout=10.0)\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "command_timeout" in violations[0]

    def test_connect_with_both_timeouts_is_clean(self, tmp_path):
        source = "import asyncpg\n" "async def f():\n" "    return await asyncpg.connect(dsn='x', timeout=10.0, command_timeout=30.0)\n"
        assert self._run(tmp_path, source) == []

    def test_lazy_function_local_import_still_resolves(self, tmp_path):
        # Matches postgres.py's own shape: `import asyncpg` inside the
        # function body, not at module scope.
        source = "async def initialize():\n" "    import asyncpg\n" "    return await asyncpg.create_pool(dsn='x', min_size=1, max_size=8)\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1

    def test_aliased_module_import_resolves(self, tmp_path):
        source = "import asyncpg as apg\n" "async def f():\n" "    return await apg.create_pool(dsn='x')\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "command_timeout" in violations[0]

    def test_from_import_call_resolves(self, tmp_path):
        source = "from asyncpg import create_pool\n" "async def f():\n" "    return await create_pool(dsn='x')\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1

    def test_aliased_from_import_call_resolves(self, tmp_path):
        source = "from asyncpg import connect as pg_connect\n" "async def f():\n" "    return await pg_connect(dsn='x')\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "timeout=" in violations[0]

    def test_unrelated_module_same_name_not_flagged(self, tmp_path):
        # `create_pool` here belongs to some OTHER module -- no `asyncpg`
        # import anywhere in the file -- so import-aware resolution must
        # NOT match it. A naive string-match on `.attr == "create_pool"`
        # would false-positive on exactly this shape.
        source = "from some_other_db_lib import create_pool\n" "async def f():\n" "    return await create_pool(dsn='x')\n"
        assert self._run(tmp_path, source) == []

    def test_unimported_asyncpg_attribute_access_not_flagged(self, tmp_path):
        # A local variable named `asyncpg` that happens to have a
        # `.create_pool` attribute, with no `import asyncpg` in the file
        # at all -- must not be matched either.
        source = "async def f(asyncpg):\n" "    return await asyncpg.create_pool(dsn='x')\n"
        assert self._run(tmp_path, source) == []

    def test_bare_kwargs_unpack_not_flagged(self, tmp_path):
        # The timeout kwarg may be supplied inside `**opts` at runtime --
        # cannot be verified statically, so this is deliberately skipped
        # rather than guessed at (see the comment in
        # scan_asyncpg_timeout_kwargs).
        source = "import asyncpg\n" "async def f(opts):\n" "    return await asyncpg.create_pool(dsn='x', **opts)\n"
        assert self._run(tmp_path, source) == []

    def test_whitelisted_line_is_skipped(self, tmp_path, monkeypatch):
        source = "import asyncpg\n" "async def f():\n" "    return await asyncpg.create_pool(dsn='x')\n"
        p = tmp_path / "synthetic.py"
        p.write_text(source, encoding="utf-8")
        monkeypatch.setattr(sys.modules[__name__], "_WHITELIST", {"synthetic.py::3"})
        assert scan_asyncpg_timeout_kwargs(p, "synthetic.py") == []

    def test_dotted_submodule_import_resolves(self, tmp_path):
        # `import asyncpg.pool` binds the top-level name `asyncpg` exactly
        # like `import asyncpg` does -- must still be recognized.
        source = "import asyncpg.pool\n" "async def f():\n" "    return await asyncpg.create_pool(dsn='x', min_size=1, max_size=8)\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "command_timeout" in violations[0]

    def test_from_dotted_submodule_import_resolves(self, tmp_path):
        source = "from asyncpg.pool import create_pool\n" "async def f():\n" "    return await create_pool(dsn='x')\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "command_timeout" in violations[0]

    def test_command_timeout_none_still_fires(self, tmp_path):
        # `command_timeout=None` is asyncpg's own "no timeout enforced"
        # sentinel -- functionally identical to omitting the kwarg, so it
        # must still be flagged, not treated as a satisfied requirement.
        source = "import asyncpg\n" "async def f():\n" "    return await asyncpg.create_pool(dsn='x', min_size=1, max_size=8, command_timeout=None)\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "command_timeout" in violations[0]

    def test_connect_timeout_none_still_fires(self, tmp_path):
        source = "import asyncpg\n" "async def f():\n" "    return await asyncpg.connect(dsn='x', timeout=None, command_timeout=None)\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
