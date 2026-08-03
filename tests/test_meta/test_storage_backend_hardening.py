"""Static regression guards for storage-backend security/correctness
invariants that were fixed during this repo's audit-remediation pass on
``src/llm_bench/storage/postgres.py`` (PostgresStorage) and
``src/llm_bench/storage/file.py`` (FileStorage). Each check below pins
down one finding so a future edit can't silently regress it without a
scanner failure calling it out by name:

  1. schema_name identifier validation -- PostgresStorage.__init__ must
     validate ``schema_name`` against a strict identifier allowlist (or
     ``.isidentifier()``) and raise before it's ever f-string-
     interpolated into any of the DDL/DML statements in this file
     (SQL-injection-shaped gap otherwise -- schema_name is operator-
     supplied, and every "# nosec B608" suppression in postgres.py is
     backed by this check running first).
  2. experiment_tag path-traversal guard -- FileStorage must validate
     any experiment_tag-shaped variable (allowlist + resolved-
     containment check) before joining it onto ``self.root`` to build a
     filesystem path. A crafted tag otherwise escapes ``root`` for
     arbitrary file write, and (via delete_experiment's
     ``shutil.rmtree``) arbitrary recursive delete.
  3. record_call()'s ON CONFLICT upsert for benchmark_results must be
     DO UPDATE, not DO NOTHING -- a bare DO NOTHING silently discards a
     successful retry that follows a prior recorded failure, permanently
     shadowing the success behind the stale failure.
  4. asyncpg.create_pool(...) must set command_timeout -- otherwise one
     hung query can exhaust the whole connection pool with no
     self-recovery.
  5. The schema DDL (``_ddl()``) must declare an index actually covering
     prefetch_resume_cache()'s hot-path WHERE clause (response IS NOT
     NULL / error_class IS NULL-or-empty), and a composite
     (experiment_tag, stage) index for query_rows()'s hot path --
     otherwise both degrade to a full sequential scan of a table that
     grows without bound across every run ever executed.
  6. Every asyncpg server-side cursor (``<conn>.cursor(...)``) must be
     opened inside ``async with <conn>.transaction():`` -- asyncpg
     raises NoActiveSQLTransactionError otherwise. delete_experiment()'s
     two DELETE statements must additionally share ONE transaction
     block (not two separate ones), so a crash between them can't leave
     benchmark_results and benchmark_winners inconsistent.

Whitelist a confirmed false positive via ``_WHITELIST`` below, keyed
``"<check_name>::<lineno>"``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_STORAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench" / "storage"
_POSTGRES_PATH = _STORAGE_ROOT / "postgres.py"
_FILE_PATH = _STORAGE_ROOT / "file.py"

# Per-check whitelist: "<check_name>::<lineno>" entries skipped.
_WHITELIST: set[str] = set()


# ------------------------------------------------------------------------
# Shared AST helpers
# ------------------------------------------------------------------------

def _parse(path: Path) -> ast.AST | None:
    """Return the parsed tree, or None (caller reports the SyntaxError)."""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _joined_static_text(node: ast.expr) -> str:
    """Concatenate every statically-known string literal fragment
    reachable from ``node`` -- plain ``Constant`` strings, the literal
    text segments of an f-string (``JoinedStr``, skipping the
    interpolated ``{expr}`` parts, whose runtime value can't be known
    statically), and ``+``-concatenation chains of the above. Adjacent
    string-literal concatenation (``f"a" f"b"``) is already merged into
    one ``JoinedStr``/``Constant`` by the parser, so it needs no special
    handling here.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _joined_static_text(node.left) + _joined_static_text(node.right)
    return ""


def _find_method(tree: ast.AST, name: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]


# ------------------------------------------------------------------------
# Check 1: schema_name identifier validation
# ------------------------------------------------------------------------

def scan_schema_name_validation(path: Path) -> list[str]:
    """Every ``__init__`` accepting a ``schema_name`` parameter must
    validate it (fullmatch/match/isidentifier referencing the parameter)
    AND raise -- otherwise it's a SQL-injection-shaped gap the moment
    schema_name reaches an f-string-interpolated query."""
    try:
        tree = _parse(path)
    except SyntaxError as e:
        return [f"{path.name}: SyntaxError: {e}"]
    assert tree is not None

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "__init__":
            continue
        params = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
        if "schema_name" not in params:
            continue
        has_validation = False
        has_raise = False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Raise):
                has_raise = True
            if isinstance(sub, ast.Call):
                func = sub.func
                attr = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
                if attr in {"fullmatch", "match", "isidentifier"}:
                    names_in_call = {n.id for n in ast.walk(sub) if isinstance(n, ast.Name)}
                    if "schema_name" in names_in_call:
                        has_validation = True
        if not (has_validation and has_raise):
            key = f"schema_name_validation::{node.lineno}"
            if key in _WHITELIST:
                continue
            violations.append(
                f"{path.name}:{node.lineno}: __init__(schema_name=...) has no "
                f"fullmatch/isidentifier check on schema_name paired with a "
                f"raise -- add e.g. `if not _SCHEMA_NAME_RE.fullmatch("
                f"schema_name): raise ValueError(...)` before schema_name is "
                f"used in any query (SQL-injection-shaped gap)."
            )
    return violations


# ------------------------------------------------------------------------
# Check 2: experiment_tag path-traversal guard
# ------------------------------------------------------------------------

def scan_experiment_tag_guard(path: Path) -> list[str]:
    """Every ``self.root / <variable>`` join must occur inside a function
    that also calls a ``validate*tag``-shaped helper AND an
    ``is_relative_to`` containment check -- otherwise a crafted
    experiment_tag can escape ``root`` (arbitrary file write / arbitrary
    recursive delete)."""
    try:
        tree = _parse(path)
    except SyntaxError as e:
        return [f"{path.name}: SyntaxError: {e}"]
    assert tree is not None

    parents = _parent_map(tree)

    def _enclosing_function(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        cur: ast.AST | None = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
            cur = parents.get(cur)
        return None

    violations: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        left = node.left
        right = node.right
        is_root_join = isinstance(left, ast.Attribute) and left.attr == "root" and isinstance(left.value, ast.Name) and left.value.id == "self"
        if not is_root_join:
            continue
        if isinstance(right, ast.Constant):
            continue  # hardcoded path segment -- not attacker-controlled
        fn = _enclosing_function(node)
        if fn is None:
            key = f"experiment_tag_guard::{node.lineno}"
            if key in _WHITELIST:
                continue
            violations.append(f"{path.name}:{node.lineno}: self.root / <var> join outside any function -- cannot verify a guard.")
            continue
        calls_in_fn = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        has_validate_call = any(
            (isinstance(c.func, ast.Name) and "validate" in c.func.id.lower() and "tag" in c.func.id.lower())
            or (isinstance(c.func, ast.Attribute) and "validate" in c.func.attr.lower() and "tag" in c.func.attr.lower())
            for c in calls_in_fn
        )
        has_containment_check = any(isinstance(c.func, ast.Attribute) and c.func.attr == "is_relative_to" for c in calls_in_fn)
        if not (has_validate_call and has_containment_check):
            key = f"experiment_tag_guard::{fn.lineno}"
            if key in _WHITELIST:
                continue
            violations.append(
                f"{path.name}:{fn.lineno}: {fn.name}() joins self.root with a "
                f"variable (line {node.lineno}) without both a "
                f"validate_experiment_tag()-style call and an "
                f".is_relative_to() containment check in the same function -- "
                f"path-traversal-shaped gap."
            )
    return violations


# ------------------------------------------------------------------------
# Check 3: record_call() upserts (DO UPDATE, never DO NOTHING)
# ------------------------------------------------------------------------

def scan_record_call_upsert(path: Path) -> list[str]:
    """record_call()'s INSERT ... ON CONFLICT for benchmark_results must
    resolve to DO UPDATE, never DO NOTHING -- DO NOTHING silently drops a
    successful retry that follows a prior recorded failure."""
    try:
        tree = _parse(path)
    except SyntaxError as e:
        return [f"{path.name}: SyntaxError: {e}"]
    assert tree is not None

    violations: list[str] = []
    for fn in _find_method(tree, "record_call"):
        found_conflict_clause = False
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            attr = func.attr if isinstance(func, ast.Attribute) else ""
            if attr != "execute" or not call.args:
                continue
            text = _joined_static_text(call.args[0]).upper()
            if "ON CONFLICT" not in text:
                continue
            found_conflict_clause = True
            if "DO NOTHING" in text:
                key = f"record_call_upsert::{call.lineno}"
                if key in _WHITELIST:
                    continue
                violations.append(
                    f"{path.name}:{call.lineno}: record_call()'s ON CONFLICT "
                    f"clause uses DO NOTHING -- a successful retry after a "
                    f"prior failure is silently discarded. Use DO UPDATE SET "
                    f"... (see persist_winners() in this file for the "
                    f"pattern)."
                )
            elif "DO UPDATE" not in text:
                key = f"record_call_upsert::{call.lineno}"
                if key in _WHITELIST:
                    continue
                violations.append(
                    f"{path.name}:{call.lineno}: record_call()'s ON CONFLICT "
                    f"clause has neither DO UPDATE nor DO NOTHING -- verify "
                    f"the upsert semantics are intentional."
                )
        if not found_conflict_clause:
            key = f"record_call_upsert::{fn.lineno}"
            if key in _WHITELIST:
                continue
            violations.append(
                f"{path.name}:{fn.lineno}: record_call() has no ON CONFLICT "
                f"clause on its INSERT -- a retry against an existing PK will "
                f"raise UniqueViolationError instead of upserting."
            )
    return violations


# ------------------------------------------------------------------------
# Check 4: asyncpg.create_pool(...) sets command_timeout
# ------------------------------------------------------------------------

def scan_command_timeout(path: Path) -> list[str]:
    """asyncpg.create_pool(...) must set command_timeout -- otherwise a
    single hung query can exhaust the whole connection pool with no
    self-recovery."""
    try:
        tree = _parse(path)
    except SyntaxError as e:
        return [f"{path.name}: SyntaxError: {e}"]
    assert tree is not None

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_create_pool = isinstance(func, ast.Attribute) and func.attr == "create_pool" and isinstance(func.value, ast.Name) and func.value.id == "asyncpg"
        if not is_create_pool:
            continue
        kw_names = {kw.arg for kw in node.keywords}
        if "command_timeout" not in kw_names:
            key = f"command_timeout::{node.lineno}"
            if key in _WHITELIST:
                continue
            violations.append(
                f"{path.name}:{node.lineno}: asyncpg.create_pool(...) has no "
                f"command_timeout kwarg -- a hung query can exhaust the whole "
                f"pool with no self-recovery. Add command_timeout=<seconds> "
                f"(30.0 is a reasonable default)."
            )
    return violations


# ------------------------------------------------------------------------
# Check 5: resume-cache / tag+stage index coverage in the DDL
# ------------------------------------------------------------------------

def scan_resume_cache_index(path: Path) -> list[str]:
    """The ``_ddl()`` function must declare an index actually covering
    prefetch_resume_cache()'s hot-path WHERE clause (response IS NOT
    NULL / error_class IS NULL-or-empty), and a composite
    (experiment_tag, stage) index for query_rows()'s hot path --
    otherwise both degrade to a full sequential scan of a table that
    grows without bound."""
    try:
        tree = _parse(path)
    except SyntaxError as e:
        return [f"{path.name}: SyntaxError: {e}"]
    assert tree is not None

    ddl_funcs = _find_method(tree, "_ddl")
    if not ddl_funcs:
        return [f"{path.name}: no _ddl() function found -- cannot verify index coverage. Add one or update this checker's target name."]

    violations: list[str] = []
    for fn in ddl_funcs:
        parts = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        ddl_compact = re.sub(r"\s+", "", "".join(parts).upper())

        has_resume_cache_index = "CREATEINDEX" in ddl_compact and "RESPONSEISNOTNULL" in ddl_compact and "ERROR_CLASS" in ddl_compact
        if not has_resume_cache_index:
            key = f"resume_cache_index::{fn.lineno}"
            if key not in _WHITELIST:
                violations.append(
                    f"{path.name}:{fn.lineno}: no CREATE INDEX statement in "
                    f"_ddl() covers prefetch_resume_cache()'s WHERE clause "
                    f"(response IS NOT NULL AND error_class IS NULL/''). Add "
                    f"a partial index on benchmark_results matching that "
                    f"predicate."
                )

        has_tag_stage_index = "CREATEINDEX" in ddl_compact and "(EXPERIMENT_TAG,STAGE)" in ddl_compact
        if not has_tag_stage_index:
            key = f"tag_stage_index::{fn.lineno}"
            if key not in _WHITELIST:
                violations.append(
                    f"{path.name}:{fn.lineno}: no composite CREATE INDEX ON "
                    f"benchmark_results (experiment_tag, stage) found in "
                    f"_ddl() -- query_rows()'s stage-filtered hot path falls "
                    f"back to a Bitmap-AND of two single-column indexes "
                    f"instead of one direct composite lookup. Add it."
                )
    return violations


# ------------------------------------------------------------------------
# Check 6: transaction-wrap consistency
# ------------------------------------------------------------------------

def _enclosing_transaction_with(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AsyncWith | None:
    """Walk ``node``'s ancestors for the nearest ``async with
    <x>.transaction():`` block, or None if it's unwrapped."""
    cur: ast.AST | None = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.AsyncWith):
            for item in cur.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute) and ctx.func.attr == "transaction":
                    return cur
        cur = parents.get(cur)
    return None


def _scan_unwrapped_cursors(tree: ast.AST, path: Path, parents: dict[ast.AST, ast.AST]) -> list[str]:
    """Rule A: every ``<x>.cursor(...)`` call must be transaction-wrapped."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "cursor"):
            continue
        if _enclosing_transaction_with(node, parents) is not None:
            continue
        key = f"cursor_wrap::{node.lineno}"
        if key in _WHITELIST:
            continue
        violations.append(
            f"{path.name}:{node.lineno}: server-side cursor (.cursor(...)) "
            f"opened without an enclosing `async with <conn>.transaction():` "
            f"-- asyncpg raises NoActiveSQLTransactionError against a live "
            f"server."
        )
    return violations


def _delete_calls_in(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    out: list[ast.Call] = []
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        attr = func.attr if isinstance(func, ast.Attribute) else ""
        if attr not in {"execute", "fetchval", "fetchrow", "fetch"} or not call.args:
            continue
        if "DELETE FROM" in _joined_static_text(call.args[0]).upper():
            out.append(call)
    return out


def _scan_delete_experiment_transaction(fn: ast.FunctionDef | ast.AsyncFunctionDef, path: Path, parents: dict[ast.AST, ast.AST]) -> list[str]:
    """Rule B: delete_experiment()'s DELETE statements must share ONE
    transaction block, not each be wrapped (or unwrapped) independently."""
    delete_calls = _delete_calls_in(fn)
    if len(delete_calls) < 2:
        key = f"delete_experiment_count::{fn.lineno}"
        if key in _WHITELIST:
            return []
        return [
            f"{path.name}:{fn.lineno}: delete_experiment() has "
            f"{len(delete_calls)} DELETE statement(s), expected 2 "
            f"(benchmark_results + benchmark_winners) -- verify nothing was "
            f"dropped."
        ]
    wraps = [_enclosing_transaction_with(c, parents) for c in delete_calls]
    if any(w is None for w in wraps):
        key = f"delete_experiment_wrap::{fn.lineno}"
        if key in _WHITELIST:
            return []
        return [f"{path.name}:{fn.lineno}: delete_experiment() has a DELETE " f"statement not wrapped in `async with <conn>.transaction():`."]
    if len({id(w) for w in wraps}) != 1:
        key = f"delete_experiment_shared::{fn.lineno}"
        if key in _WHITELIST:
            return []
        return [
            f"{path.name}:{fn.lineno}: delete_experiment()'s DELETE "
            f"statements are wrapped in SEPARATE transaction blocks, not one "
            f"shared transaction -- a crash between them can leave "
            f"benchmark_results/benchmark_winners inconsistent."
        ]
    return []


def scan_transaction_wrap(path: Path) -> list[str]:
    """Every asyncpg server-side cursor (``<conn>.cursor(...)``) must be
    opened inside ``async with <conn>.transaction():`` -- asyncpg raises
    NoActiveSQLTransactionError otherwise. delete_experiment()'s two
    DELETE statements must additionally share ONE transaction block, not
    two separate ones, so a crash between them can't leave
    benchmark_results and benchmark_winners inconsistent."""
    try:
        tree = _parse(path)
    except SyntaxError as e:
        return [f"{path.name}: SyntaxError: {e}"]
    assert tree is not None

    parents = _parent_map(tree)
    violations = _scan_unwrapped_cursors(tree, path, parents)
    for fn in _find_method(tree, "delete_experiment"):
        violations.extend(_scan_delete_experiment_transaction(fn, path, parents))
    return violations


# ------------------------------------------------------------------------
# Tests against the real files
# ------------------------------------------------------------------------

def test_schema_name_validation_enforced():
    violations = scan_schema_name_validation(_POSTGRES_PATH)
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"schema_name validation gap in postgres.py:\n  {msg}")


def test_experiment_tag_guard_enforced():
    violations = scan_experiment_tag_guard(_FILE_PATH)
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"experiment_tag path-traversal guard gap in file.py:\n  {msg}")


def test_record_call_upserts_on_conflict():
    violations = scan_record_call_upsert(_POSTGRES_PATH)
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"record_call() upsert regression in postgres.py:\n  {msg}")


def test_command_timeout_configured():
    violations = scan_command_timeout(_POSTGRES_PATH)
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"asyncpg.create_pool command_timeout gap in postgres.py:\n  {msg}")


def test_resume_cache_index_present():
    violations = scan_resume_cache_index(_POSTGRES_PATH)
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"Missing index coverage in postgres.py's _ddl():\n  {msg}")


def test_transaction_wrap_consistency():
    violations = scan_transaction_wrap(_POSTGRES_PATH)
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"Transaction-wrap inconsistency in postgres.py:\n  {msg}")


# ------------------------------------------------------------------------
# Regression tests for the checkers themselves (synthetic source, tmp_path)
# ------------------------------------------------------------------------

class TestSchemaNameValidationDetectsShapes:
    def _run(self, tmp_path, source: str) -> list[str]:
        p = tmp_path / "synthetic_postgres.py"
        p.write_text(source, encoding="utf-8")
        return scan_schema_name_validation(p)

    def test_missing_validation_fires(self, tmp_path):
        source = (
            "class Storage:\n" "    def __init__(self, url, *, schema_name='llm_bench'):\n" "        self._url = url\n" "        self._schema = schema_name\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 1

    def test_validation_with_raise_is_clean(self, tmp_path):
        source = (
            "import re\n"
            "_SCHEMA_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')\n"
            "class Storage:\n"
            "    def __init__(self, url, *, schema_name='llm_bench'):\n"
            "        if not _SCHEMA_NAME_RE.fullmatch(schema_name):\n"
            "            raise ValueError('bad schema_name')\n"
            "        self._url = url\n"
            "        self._schema = schema_name\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []

    def test_init_without_schema_name_param_is_ignored(self, tmp_path):
        source = "class Storage:\n" "    def __init__(self, url):\n" "        self._url = url\n"
        violations = self._run(tmp_path, source)
        assert violations == []


class TestExperimentTagGuardDetectsShapes:
    def _run(self, tmp_path, source: str) -> list[str]:
        p = tmp_path / "synthetic_file.py"
        p.write_text(source, encoding="utf-8")
        return scan_experiment_tag_guard(p)

    def test_unvalidated_join_fires(self, tmp_path):
        source = (
            "from pathlib import Path\n"
            "class FileStorage:\n"
            "    def __init__(self, root):\n"
            "        self.root = Path(root)\n"
            "    def _tag_dir(self, experiment_tag):\n"
            "        return self.root / experiment_tag\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 1

    def test_validated_join_is_clean(self, tmp_path):
        source = (
            "from pathlib import Path\n"
            "def validate_experiment_tag(tag):\n"
            "    return tag\n"
            "class FileStorage:\n"
            "    def __init__(self, root):\n"
            "        self.root = Path(root)\n"
            "    def _tag_dir(self, experiment_tag):\n"
            "        validate_experiment_tag(experiment_tag)\n"
            "        root_resolved = self.root.resolve()\n"
            "        candidate = (self.root / experiment_tag).resolve()\n"
            "        if not candidate.is_relative_to(root_resolved):\n"
            "            raise ValueError('escapes root')\n"
            "        return candidate\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []

    def test_constant_join_is_ignored(self, tmp_path):
        source = (
            "from pathlib import Path\n"
            "class FileStorage:\n"
            "    def __init__(self, root):\n"
            "        self.root = Path(root)\n"
            "    def _index_dir(self):\n"
            "        return self.root / '_index'\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []


class TestRecordCallUpsertDetectsShapes:
    def _run(self, tmp_path, source: str) -> list[str]:
        p = tmp_path / "synthetic_postgres.py"
        p.write_text(source, encoding="utf-8")
        return scan_record_call_upsert(p)

    def test_do_nothing_fires(self, tmp_path):
        source = (
            "class Storage:\n"
            "    async def record_call(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_results (a) VALUES ($1) '\n"
            "                f'ON CONFLICT (a) DO NOTHING',\n"
            "                row.a,\n"
            "            )\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 1

    def test_do_update_is_clean(self, tmp_path):
        source = (
            "class Storage:\n"
            "    async def record_call(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_results (a) VALUES ($1) '\n"
            "                f'ON CONFLICT (a) DO UPDATE SET a=EXCLUDED.a',\n"
            "                row.a,\n"
            "            )\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []

    def test_missing_on_conflict_fires(self, tmp_path):
        source = (
            "class Storage:\n"
            "    async def record_call(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_results (a) VALUES ($1)',\n"
            "                row.a,\n"
            "            )\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 1


class TestCommandTimeoutDetectsShapes:
    def _run(self, tmp_path, source: str) -> list[str]:
        p = tmp_path / "synthetic_postgres.py"
        p.write_text(source, encoding="utf-8")
        return scan_command_timeout(p)

    def test_missing_command_timeout_fires(self, tmp_path):
        source = "async def initialize(self):\n" "    import asyncpg\n" "    pool = await asyncpg.create_pool(dsn=self._url, min_size=1, max_size=8)\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1

    def test_present_command_timeout_is_clean(self, tmp_path):
        source = (
            "async def initialize(self):\n"
            "    import asyncpg\n"
            "    pool = await asyncpg.create_pool(dsn=self._url, min_size=1, max_size=8, command_timeout=30.0)\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []


class TestResumeCacheIndexDetectsShapes:
    def _run(self, tmp_path, source: str) -> list[str]:
        p = tmp_path / "synthetic_postgres.py"
        p.write_text(source, encoding="utf-8")
        return scan_resume_cache_index(p)

    def test_missing_indexes_fire_both(self, tmp_path):
        source = (
            "def _ddl(schema):\n"
            "    s = schema\n"
            "    return [\n"
            "        f'CREATE TABLE IF NOT EXISTS {s}.benchmark_results (composite_hash TEXT);',\n"
            "        f'CREATE INDEX IF NOT EXISTS idx_tag ON {s}.benchmark_results (experiment_tag);',\n"
            "    ]\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 2

    def test_both_indexes_present_is_clean(self, tmp_path):
        source = (
            "def _ddl(schema):\n"
            "    s = schema\n"
            "    return [\n"
            "        f'CREATE INDEX IF NOT EXISTS idx_tag_stage ON {s}.benchmark_results (experiment_tag, stage);',\n"
            "        \"\"\"\n"
            "        CREATE INDEX IF NOT EXISTS idx_resume_cache\n"
            "            ON benchmark_results (composite_hash)\n"
            "            WHERE response IS NOT NULL AND (error_class IS NULL OR error_class = '');\n"
            "        \"\"\",\n"
            "    ]\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []

    def test_no_ddl_function_fires(self, tmp_path):
        source = "def not_ddl():\n" "    return []\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1


class TestTransactionWrapDetectsShapes:
    def _run(self, tmp_path, source: str) -> list[str]:
        p = tmp_path / "synthetic_postgres.py"
        p.write_text(source, encoding="utf-8")
        return scan_transaction_wrap(p)

    def test_unwrapped_cursor_fires(self, tmp_path):
        source = "class Storage:\n" "    async def query_rows(self, conn, sql):\n" "        async for r in conn.cursor(sql):\n" "            yield r\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1

    def test_wrapped_cursor_is_clean(self, tmp_path):
        source = (
            "class Storage:\n"
            "    async def query_rows(self, conn, sql):\n"
            "        async with conn.transaction():\n"
            "            async for r in conn.cursor(sql):\n"
            "                yield r\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []

    def test_delete_experiment_separate_transactions_fires(self, tmp_path):
        source = (
            "class Storage:\n"
            "    async def delete_experiment(self, conn, tag):\n"
            "        async with conn.transaction():\n"
            "            n = await conn.fetchval('DELETE FROM benchmark_results WHERE experiment_tag=$1', tag)\n"
            "        async with conn.transaction():\n"
            "            await conn.execute('DELETE FROM benchmark_winners WHERE experiment_tag=$1', tag)\n"
            "        return n\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 1

    def test_delete_experiment_shared_transaction_is_clean(self, tmp_path):
        source = (
            "class Storage:\n"
            "    async def delete_experiment(self, conn, tag):\n"
            "        async with conn.transaction():\n"
            "            n = await conn.fetchval('DELETE FROM benchmark_results WHERE experiment_tag=$1', tag)\n"
            "            await conn.execute('DELETE FROM benchmark_winners WHERE experiment_tag=$1', tag)\n"
            "        return n\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []
