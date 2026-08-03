"""Static regression guard: within-class resource-guard (lock/transaction)
consistency.

Motivating finding (already fixed in this repo's audit-remediation pass;
see storage/postgres.py's inline commentary and checks 2/6 in
test_storage_backend_hardening.py): PostgresStorage.prefetch_resume_cache()
used to call conn.cursor(sql, *params) WITHOUT wrapping it in
``async with conn.transaction():``, a guaranteed
asyncpg.exceptions.NoActiveSQLTransactionError on every real Postgres
call, while query_rows(), 50 lines later in the SAME file, did the
identical conn.cursor() pattern correctly wrapped. Also:
delete_experiment()'s two DELETEs weren't wrapped in conn.transaction()
while upsert_prompts()'s three related INSERTs, in the same file, were.

Both are fixed today and pinned down by name in
test_storage_backend_hardening.py. THIS scanner generalizes the pattern
that produced them: within one class, the SAME operation-shape (a Call
matched by its attribute-chain + method name, e.g. ``<var>.cursor`` or
``self._db.execute``, generalized over the local variable name that
holds the connection/cursor/pool, but NOT over self-attribute chains,
since those name a specific instance attribute, not an interchangeable
local) must be consistently guard-wrapped: either ALWAYS inside
``async with <x>.transaction():`` / ``async with self._lock:``, or NEVER.
A method that guards an operation-shape while a sibling method in the
same class performs the identical shape unguarded is exactly this bug's
signature: a guard added at one call site and never propagated to the
other(s).

Running this scanner against the current tree found one such gap that
had NOT yet been examined by the prior audit pass: FileStorage's
persist_winners()/load_winners()/delete_experiment() used
``asyncio.to_thread(...)`` for their filesystem I/O WITHOUT going through
self._lock, while record_call()/upsert_prompts()/query_rows()'s
identical-shape to_thread calls, in the same file, did. Concretely,
delete_experiment()'s ``shutil.rmtree(tag_dir)`` ran AFTER self._lock was
released, so a concurrent persist_winners()/record_call() for the same
tag (itself lock-serialised) could still be mid-write while the rmtree
deleted the directory out from under it. Fixed directly in
storage/file.py (all three methods now take self._lock around their
filesystem access) rather than whitelisted: this was a real gap in the
class's own documented invariant ("ONE lock guards every operation", see
FileStorage's class docstring), not a legitimate by-design asymmetry.

Scope: every *.py file under src/llm_bench that actually uses a
lock/transaction guard (contains "asyncio.Lock", "self._lock", or
".transaction(") - found by grepping the whole tree, not assumed to be
only storage/postgres.py. As of this writing that's storage/postgres.py,
storage/file.py, storage/memory.py, runner/budget.py, runner/resume.py;
a file with none of those markers is skipped before it's even parsed
(cheap substring gate), so a NEW file that starts using a lock/
transaction is picked up automatically without touching this scanner.

Real false-positive risk: plenty of asymmetry is legitimate by design.
A read-only single-statement query doesn't need a transaction (asyncpg
auto-commits a lone execute/fetchrow/fetchval) even though a
multi-statement write does; a connection pool's self._lock guards lazy
pool CREATION/teardown, not every individual acquire() (locking every
acquire would serialise the whole pool, defeating its purpose: see
PostgresStorage's own class docstring on max_connections sizing);
Path.mkdir(parents=True, exist_ok=True) is race-safe by construction so
it never needs a lock either; and a shared retry/read helper's own
internal call (``_db_execute_with_retry``, ``_read_bytes_or_none``) is
invisible to this AST-only, single-function-scope scanner even when
every real caller invokes it from inside the lock.

Known scanner limitations (kept deliberately narrow rather than chasing
full data-flow analysis):
- Shape comparison is restricted to a curated set of resource-operation
  method names (``_RESOURCE_OP_NAMES``: cursor/execute/fetch*/commit/
  acquire/mkdir/rmtree/to_thread/... ) rather than every attribute-call
  in a class, so an unrelated call (e.g. ``logger.info(...)``) that
  happens to differ in lock-nesting between two sibling methods is never
  flagged: it isn't a resource-guard bug, so it isn't this scanner's job.
- Guard-expression alias resolution is single-hop: ``lk = self._lock;
  async with lk:`` IS recognized (the with-item's ``Name`` is resolved
  back to its last simple assignment within the same function), but an
  alias-of-an-alias (``lk = self._lock; lk2 = lk; async with lk2:``) or
  an alias assigned inside a branch/loop with control-flow-dependent
  reassignment is not chased further; a full data-flow analysis was
  judged not worth the complexity for a lint-style regression guard. So this check uses
the baseline-JSON-ratchet pattern (see test_code_audit_baseline.py /
py_ci_shared.code_audit_meta) rather than a hard assert-empty: findings
are keyed "<file>::<class>::<shape>" and diffed against a committed
baseline; only a NEW finding (a class+shape pair that wasn't already
asymmetric) fails the test. Refresh after a deliberate, reviewed change:

    pytest tests/test_meta/test_asymmetric_resource_guards.py --refresh-asymmetric-resource-guards-baseline

Confirmed-by-design asymmetries (the ones enumerated above) never even
enter the baseline: whitelisted outright below, each with a one-line
rationale. As of this writing the checker finds no OTHER asymmetry in
the current tree, so the committed baseline is an empty list.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

import orjson
import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench"
_BASELINE_PATH = Path(__file__).resolve().parent / "_asymmetric_resource_guards_baseline.json"
_REFRESH_FLAG = "--refresh-asymmetric-resource-guards-baseline"

# Cheap substring gate before paying for an AST parse: a file containing
# none of these can never hold a lock/transaction guard to be asymmetric
# about, so there's nothing this scanner could ever find in it.
_CANDIDATE_MARKERS = ("asyncio.Lock", "self._lock", ".transaction(")

# Confirmed-by-design asymmetries that never enter the baseline. Keyed
# "<file.py>::<ClassName>::<shape>", each with a one-line rationale.
_WHITELIST: set[str] = {
    # A lone execute/fetchval (record_call, persist_winners,
    # query_spend_total) never needs an explicit transaction: asyncpg
    # auto-commits a single statement. Only the genuinely multi-statement
    # writes (upsert_prompts's 3 INSERTs, delete_experiment's 2 DELETEs)
    # need one for atomicity, already reasoned through in postgres.py's
    # own docstrings and test_storage_backend_hardening.py's check 6.
    "postgres.py::PostgresStorage::<var>.execute",
    "postgres.py::PostgresStorage::<var>.fetchval",
    # self._lock guards lazy POOL CREATION/teardown in initialize()/
    # close() (a TOCTOU on self._pool), never individual pool.acquire()
    # calls: guarding every acquire() would serialise the whole
    # connection pool on every single query, defeating the entire point
    # of max_connections > 1 (see PostgresStorage's class docstring on
    # max_connections sizing vs global_concurrency).
    "postgres.py::PostgresStorage::<var>.acquire",
    # Path.mkdir(parents=True, exist_ok=True) is idempotent/race-safe by
    # construction (Python catches the OS's FileExistsError and verifies
    # the existing path is a directory), the standard idiom for exactly
    # concurrent directory creation, so it needs no lock for correctness.
    "file.py::FileStorage::<var>.mkdir",
    # Scanner blind spot, not a bug: _db_execute_with_retry() is a shared
    # retry helper whose OWN self._db.execute(...) call is never lexically
    # wrapped in a with-block (it's a separate function body), but every
    # real caller (upsert_prompts, record_call, delete_experiment) invokes
    # it from inside self._lock: the guard IS there at runtime, just one
    # function-call hop away from where this AST-only scanner can see it.
    "file.py::FileStorage::self._db.execute",
    # Same scanner blind spot as above, for the other shared helper:
    # _read_bytes_or_none()'s own path.exists() call is never lexically
    # wrapped, but its only caller (load_winners) invokes it from inside
    # self._lock.
    "file.py::FileStorage::<var>.exists",
}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


# Curated set of resource-operation-like method names: only calls whose
# method name lands here participate in shape comparison at all. This
# keeps the scanner scoped to DB/lock/filesystem operations (the class of
# bug it exists to catch) instead of comparing lock-nesting for EVERY
# attribute-call in a class (logging, metrics, arbitrary helpers), which
# would flag irrelevant asymmetries (e.g. `logger.info(...)` called inside
# self._lock in one method and outside it in a sibling) as if they were
# real transaction/lock bugs.
_RESOURCE_OP_NAMES = {
    # DB / asyncpg
    "cursor",
    "execute",
    "executemany",
    "fetch",
    "fetchval",
    "fetchrow",
    "fetchmany",
    "commit",
    "rollback",
    "acquire",
    "release",
    "transaction",
    # filesystem
    "mkdir",
    "rmtree",
    "unlink",
    "remove",
    "rename",
    "replace",
    "copy",
    "copy2",
    "move",
    "open",
    "exists",
    "read_bytes",
    "write_bytes",
    "read_text",
    "write_text",
    "to_thread",
}


def _call_shape(call: ast.Call) -> str | None:
    """Normalized operation-shape key for a Call node: <expr>.<method>(...),
    generalized over the LOCAL VARIABLE NAME holding the base object
    (``conn``, ``c``, ``connection`` all collapse to ``<var>``, so
    ``conn.cursor(...)`` and ``c.cursor(...)`` are the SAME shape) but NOT
    over ``self``-attribute chains (``self._pool.acquire()`` and
    ``self._db.execute()`` stay DISTINCT shapes: each names a specific
    instance attribute, not an interchangeable local).

    Returns None for calls that aren't a simple
    ``<name-or-self-chain>.<method>(...)`` shape (bare function calls,
    subscripts, nested calls as the base, ...), or whose method name isn't
    in the curated ``_RESOURCE_OP_NAMES`` set: nothing useful (or safe from
    noise) to compare those across methods.
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    method_name = func.attr
    if method_name not in _RESOURCE_OP_NAMES:
        return None
    chain: list[str] = []
    base: ast.expr = func.value
    while isinstance(base, ast.Attribute):
        chain.append(base.attr)
        base = base.value
    if not isinstance(base, ast.Name):
        return None  # base is itself a call/subscript/etc: too dynamic to key on
    chain.append("self" if base.id == "self" else "<var>")
    chain.reverse()
    return ".".join([*chain, method_name])


def _build_alias_map(func: ast.AST) -> dict[str, ast.expr]:
    """Map simple local-variable names to the expression they were last
    assigned, within one function body (``lk = self._lock`` -> {"lk":
    <self._lock Attribute node>}). Single-hop only (an alias-of-an-alias
    is not chased further) and last-assignment-wins with no control-flow
    awareness: a heuristic good enough to catch the common ``x = <guard
    expr>; with x:`` refactor, not a full data-flow analysis.
    """
    aliases: dict[str, ast.expr] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            aliases[node.targets[0].id] = node.value
    return aliases


def _is_guard_context_expr(expr: ast.expr, aliases: dict[str, ast.expr] | None = None) -> bool:
    """True for ``<x>.transaction()`` or ``self._lock`` used as a
    with-item's context-manager expression (directly, or via a simple
    local-variable alias resolved through ``aliases``)."""
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "transaction":
        return True
    if isinstance(expr, ast.Attribute) and expr.attr == "_lock" and isinstance(expr.value, ast.Name) and expr.value.id == "self":
        return True
    if aliases is not None and isinstance(expr, ast.Name) and expr.id in aliases:
        return _is_guard_context_expr(aliases[expr.id], None)  # one alias hop only
    return False


def _is_guarded(node: ast.AST, parents: dict[ast.AST, ast.AST], aliases: dict[str, ast.expr]) -> bool:
    """True if any ancestor with/async with of ``node`` guards it."""
    cur: ast.AST | None = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.With, ast.AsyncWith)) and any(_is_guard_context_expr(item.context_expr, aliases) for item in cur.items):
            return True
        cur = parents.get(cur)
    return False


@dataclass
class Finding:
    file: str
    cls: str
    shape: str
    guarded_methods: list[str]
    unguarded_methods: list[str]

    @property
    def key(self) -> str:
        return f"{self.file}::{self.cls}::{self.shape}"

    def message(self) -> str:
        return (
            f"{self.file}: class {self.cls}, operation-shape {self.shape!r} is "
            f"guard-wrapped (async with <x>.transaction(): / async with "
            f"self._lock:) in {self.guarded_methods} but NOT in "
            f"{self.unguarded_methods}. Either the unguarded method(s) are "
            f"missing a guard that was added elsewhere and never propagated "
            f"(the exact shape of the prefetch_resume_cache / "
            f"delete_experiment bugs this checker guards against), or this "
            f"is a confirmed by-design asymmetry: add it to _WHITELIST with "
            f"a one-line rationale, or refresh the baseline after review."
        )


def _scan_class(path: Path, cls: ast.ClassDef, parents: dict[ast.AST, ast.AST]) -> list[Finding]:
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    # shape -> method_name -> [guarded? for each occurrence of that shape
    # found anywhere in that method's body, including nested closures]
    by_shape: dict[str, dict[str, list[bool]]] = {}
    for method in methods:
        aliases = _build_alias_map(method)
        for node in ast.walk(method):
            if not isinstance(node, ast.Call):
                continue
            shape = _call_shape(node)
            if shape is None:
                continue
            by_shape.setdefault(shape, {}).setdefault(method.name, []).append(_is_guarded(node, parents, aliases))

    findings: list[Finding] = []
    for shape, method_flags in sorted(by_shape.items()):
        # A method counts as "guarded" for this shape only if EVERY
        # occurrence of it in that method is guarded; a single unguarded
        # occurrence puts the method in the "unguarded" bucket (that's
        # the risk worth flagging), so a method can never land in both.
        guarded_methods = sorted(m for m, flags in method_flags.items() if all(flags))
        unguarded_methods = sorted(m for m, flags in method_flags.items() if not all(flags))
        if guarded_methods and unguarded_methods:
            findings.append(Finding(path.name, cls.name, shape, guarded_methods, unguarded_methods))
    return findings


def scan_file(path: Path) -> list[Finding]:
    source = path.read_text(encoding="utf-8")
    if not any(marker in source for marker in _CANDIDATE_MARKERS):
        return []
    tree = _parse(path)
    parents = _parent_map(tree)
    findings: list[Finding] = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        findings.extend(_scan_class(path, cls, parents))
    return findings


def collect_all_findings() -> list[Finding]:
    out: list[Finding] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        out.extend(scan_file(path))
    return out


def _refresh_requested() -> bool:
    return _REFRESH_FLAG in sys.argv


def test_no_new_asymmetric_resource_guards():
    by_key = {f.key: f for f in collect_all_findings() if f.key not in _WHITELIST}
    current_keys = set(by_key)

    if _refresh_requested() or not _BASELINE_PATH.exists():
        _BASELINE_PATH.write_text(
            orjson.dumps(sorted(current_keys), option=orjson.OPT_INDENT_2).decode("utf-8"),
            encoding="utf-8",
        )
        pytest.skip(f"asymmetric-resource-guards baseline refreshed at {_BASELINE_PATH.name} ({len(current_keys)} existing finding(s))")

    baseline = set(orjson.loads(_BASELINE_PATH.read_bytes()))
    new = sorted(current_keys - baseline)
    if new:
        detail = "\n  ".join(by_key[k].message() for k in new)
        pytest.fail(
            f"{len(new)} NEW asymmetric resource-guard finding(s), fix the "
            f"missing guard or update _WHITELIST/baseline after review:\n"
            f"  {detail}\n"
            f"  Refresh: pytest tests/test_meta/test_asymmetric_resource_guards.py {_REFRESH_FLAG}"
        )


# ------------------------------------------------------------------------
# Regression tests for the checker itself (synthetic source, tmp_path)
# ------------------------------------------------------------------------

class TestScanFileDetectsAsymmetricGuards:
    def _run(self, tmp_path, source: str) -> list[Finding]:
        p = tmp_path / "synthetic_storage.py"
        p.write_text(source, encoding="utf-8")
        return scan_file(p)

    def test_asymmetric_transaction_wrap_fires(self, tmp_path):
        source = (
            "class Storage:\n"
            "    async def guarded(self, conn, sql):\n"
            "        async with conn.transaction():\n"
            "            async for r in conn.cursor(sql):\n"
            "                yield r\n"
            "    async def unguarded(self, conn, sql):\n"
            "        async for r in conn.cursor(sql):\n"
            "            yield r\n"
        )
        findings = self._run(tmp_path, source)
        assert len(findings) == 1
        f = findings[0]
        assert f.shape == "<var>.cursor"
        assert f.guarded_methods == ["guarded"]
        assert f.unguarded_methods == ["unguarded"]
        assert f.key == "synthetic_storage.py::Storage::<var>.cursor"

    def test_both_methods_guarded_is_clean(self, tmp_path):
        source = (
            "class Storage:\n"
            "    async def a(self, conn, sql):\n"
            "        async with conn.transaction():\n"
            "            return await conn.cursor(sql)\n"
            "    async def b(self, conn, sql):\n"
            "        async with conn.transaction():\n"
            "            return await conn.cursor(sql)\n"
        )
        assert self._run(tmp_path, source) == []

    def test_both_methods_unguarded_is_clean(self, tmp_path):
        # Neither method guards the shape: consistent (if debatable)
        # within the class, so nothing to flag; a shape only fires when
        # it's guarded SOMEWHERE and not somewhere else.
        source = (
            "class Storage:\n"
            "    async def a(self, conn, sql):\n"
            "        return await conn.cursor(sql)\n"
            "    async def b(self, conn, sql):\n"
            "        return await conn.cursor(sql)\n"
        )
        assert self._run(tmp_path, source) == []

    def test_self_lock_recognized_as_guard(self, tmp_path):
        source = (
            "class Storage:\n"
            "    def __init__(self):\n"
            "        self._lock = None\n"
            "    async def a(self, conn):\n"
            "        async with self._lock:\n"
            "            return await conn.execute('SELECT 1')\n"
            "    async def b(self, conn):\n"
            "        return await conn.execute('SELECT 1')\n"
        )
        findings = self._run(tmp_path, source)
        assert len(findings) == 1
        assert findings[0].shape == "<var>.execute"
        assert findings[0].guarded_methods == ["a"]
        assert findings[0].unguarded_methods == ["b"]

    def test_local_var_name_generalized_across_methods(self, tmp_path):
        # 'conn' in one method and 'c' in another are the SAME shape:
        # the checker generalizes over local variable names so a mere
        # rename can't dodge it.
        source = (
            "class Storage:\n"
            "    async def a(self, conn):\n"
            "        async with conn.transaction():\n"
            "            return await conn.cursor('SELECT 1')\n"
            "    async def b(self, c):\n"
            "        return await c.cursor('SELECT 1')\n"
        )
        findings = self._run(tmp_path, source)
        assert len(findings) == 1
        assert findings[0].shape == "<var>.cursor"

    def test_self_attribute_chain_kept_distinct_from_local_var(self, tmp_path):
        # self._db.execute(...) and conn.execute(...) are DIFFERENT
        # shapes: a self-attribute chain names a specific instance
        # attribute, not an interchangeable local, so they must not be
        # conflated into a false cross-method finding.
        source = (
            "class Storage:\n"
            "    def __init__(self):\n"
            "        self._lock = None\n"
            "        self._db = None\n"
            "    async def a(self, conn):\n"
            "        async with self._lock:\n"
            "            return await self._db.execute('SELECT 1')\n"
            "    async def b(self, conn):\n"
            "        return await conn.execute('SELECT 1')\n"
        )
        assert self._run(tmp_path, source) == []

    def test_different_classes_not_conflated(self, tmp_path):
        # Same shape, asymmetric guard status, but in TWO DIFFERENT
        # classes: each class is judged independently, so no
        # cross-class false positive.
        source = (
            "class A:\n"
            "    async def a(self, conn):\n"
            "        async with conn.transaction():\n"
            "            return await conn.cursor('SELECT 1')\n"
            "class B:\n"
            "    async def b(self, conn):\n"
            "        return await conn.cursor('SELECT 1')\n"
        )
        assert self._run(tmp_path, source) == []

    def test_file_without_lock_or_transaction_marker_is_skipped(self, tmp_path):
        # No 'asyncio.Lock' / 'self._lock' / '.transaction(' anywhere in
        # the file: the cheap pre-filter must skip it before parsing,
        # since there's nothing a guard could ever wrap.
        source = "class Storage:\n" "    def plain(self):\n" "        return 1\n"
        assert self._run(tmp_path, source) == []

    def test_mixed_occurrences_within_one_method_count_as_unguarded(self, tmp_path):
        # A method with BOTH a guarded and an unguarded occurrence of the
        # same shape lands in the unguarded bucket for that method (the
        # unguarded occurrence is the real risk), still fires against a
        # sibling method that's guarded throughout.
        source = (
            "class Storage:\n"
            "    async def mixed(self, conn):\n"
            "        async with conn.transaction():\n"
            "            await conn.execute('A')\n"
            "        await conn.execute('B')\n"
            "    async def clean(self, conn):\n"
            "        async with conn.transaction():\n"
            "            await conn.execute('C')\n"
        )
        findings = self._run(tmp_path, source)
        assert len(findings) == 1
        assert findings[0].guarded_methods == ["clean"]
        assert findings[0].unguarded_methods == ["mixed"]

    def test_single_hop_alias_recognized_as_guard(self, tmp_path):
        # `lk = self._lock; async with lk:` must still be recognized as a
        # self._lock guard even though the with-item's expression is a
        # bare Name, not the literal `self._lock` Attribute.
        source = (
            "class Storage:\n"
            "    def __init__(self):\n"
            "        self._lock = None\n"
            "    async def a(self, conn):\n"
            "        lk = self._lock\n"
            "        async with lk:\n"
            "            return await conn.execute('SELECT 1')\n"
            "    async def b(self, conn):\n"
            "        return await conn.execute('SELECT 1')\n"
        )
        findings = self._run(tmp_path, source)
        assert len(findings) == 1
        assert findings[0].shape == "<var>.execute"
        assert findings[0].guarded_methods == ["a"]
        assert findings[0].unguarded_methods == ["b"]

    def test_double_hop_alias_not_chased(self, tmp_path):
        # An alias-of-an-alias is a documented limitation, not chased: the
        # double-hop method still lands in "unguarded" for that shape even
        # though it is guarded at runtime, alongside a genuinely-unguarded
        # sibling and a directly-guarded one -- pinning the CURRENT
        # (known-narrow) behavior rather than silently regressing further.
        source = (
            "class Storage:\n"
            "    def __init__(self):\n"
            "        self._lock = None\n"
            "    async def double_hop(self, conn):\n"
            "        lk = self._lock\n"
            "        lk2 = lk\n"
            "        async with lk2:\n"
            "            return await conn.execute('SELECT 1')\n"
            "    async def direct(self, conn):\n"
            "        async with self._lock:\n"
            "            return await conn.execute('SELECT 1')\n"
        )
        findings = self._run(tmp_path, source)
        assert len(findings) == 1
        assert findings[0].guarded_methods == ["direct"]
        assert findings[0].unguarded_methods == ["double_hop"]

    def test_non_resource_method_name_never_compared(self, tmp_path):
        # logger.info(...) differs in lock-nesting between two sibling
        # methods, but "info" isn't a resource-operation name, so it must
        # never be flagged: this scanner is scoped to DB/lock/filesystem
        # operations, not arbitrary attribute-calls.
        source = (
            "class Storage:\n"
            "    def __init__(self):\n"
            "        self._lock = None\n"
            "    async def a(self, logger):\n"
            "        async with self._lock:\n"
            "            logger.info('x')\n"
            "    async def b(self, logger):\n"
            "        logger.info('x')\n"
        )
        assert self._run(tmp_path, source) == []
