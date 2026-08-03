"""Regression guard for asymmetric ``ON CONFLICT`` resolution across the
storage backends' SQL (audit finding, now fixed): ``record_call()``'s
INSERT into ``benchmark_results`` used to resolve conflicts with a bare
``ON CONFLICT ... DO NOTHING``, silently discarding a successful retry
that followed one prior recorded failure -- while ``persist_winners()``'s
INSERT into ``benchmark_winners``, in the SAME file, correctly used
``ON CONFLICT ... DO UPDATE SET ...``. Both tables hold one row per
"this call/round was attempted" key, so the two statements should have
agreed on upsert semantics; nothing in the code said they were allowed to
differ. ``record_call()`` was fixed to ``DO UPDATE`` (see
``test_storage_backend_hardening.py``'s check 3, which pins that one
statement down directly). This module generalizes the pattern into a
scanner instead of a single pinned check.

Scan: regex-extract every ``INSERT INTO <table> ... ON CONFLICT (<cols>)
DO NOTHING|DO UPDATE ...`` occurrence from string literals in every
``storage/*.py`` file, and -- within one file, across ALL per-attempt
statements regardless of ON CONFLICT column-list arity (the arity is
reported in the finding as descriptive metadata only, never used to gate
the comparison -- the historical bug this scanner exists to generalize
from involved two statements with DIFFERENT arities: 4-col
``record_call()`` vs 2-col ``persist_winners()``, so gating on arity
equality would have missed it) -- flag a ``DO NOTHING`` statement that
disagrees with ANY ``DO UPDATE`` statement in the same file UNLESS either
(a) the table
looks append-only/content-addressed (single conflict column literally
named ``hash``/``*_hash``, or a table name ending in a log-ish suffix --
dedup-by-hash tables never need to overwrite, since the value for a given
key never changes), or (b) there's a nearby ``#`` comment mentioning the
conflict/upsert choice explaining why the difference is deliberate.

This is inherently a heuristic, advisory check -- legitimate tables CAN
want different upsert semantics, and the "per-attempt vs append-only"
classification is a guess from naming, not real schema introspection. So
findings are NOT a hard ``pytest.fail`` on every run; they're baselined
(keyed by ``on_conflict_asymmetry::<file>:<line>``), same mechanism as
``test_code_audit_baseline.py``/``test_api_stability.py``'s snapshot
ratchet. A pre-existing/reviewed finding sits quietly in the baseline; a
NEW one fails the test and needs a one-line human sign-off before it's
accepted -- either add a short comment near the ON CONFLICT clause
explaining the deliberate difference (which also directly silences the
scanner on the next run, no baseline edit needed), or, if a persistent
comment isn't practical, review the finding and refresh:

    pytest tests/test_meta/test_sql_on_conflict_consistency.py --refresh-sql-conflict-baseline

(flag registered in ``tests/test_meta/conftest.py``).
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import orjson
import pytest

_STORAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench" / "storage"
_BASELINE_PATH = Path(__file__).resolve().parent / "_sql_on_conflict_baseline.json"
_REFRESH_FLAG = "--refresh-sql-conflict-baseline"

# Table-name suffixes treated as "clearly append-only" regardless of the
# conflict-column heuristic below (a log/history/event table is expected
# to never overwrite an existing row's payload).
_APPEND_ONLY_TABLE_HINTS = ("_prompts", "_log", "_logs", "_event", "_events", "_history")

# A standalone "#" comment line within (or immediately around) a flagged
# statement mentioning any of these is treated as a human having already
# explained the choice -- suppresses the finding.
_EXPLANATION_MARKERS = ("conflict", "do nothing", "do update", "upsert")

_INSERT_ON_CONFLICT_RE = re.compile(
    r"INSERT\s+INTO\s+\.?(?P<table>\w+)"
    r"(?:(?!INSERT\s+INTO)[\s\S])*?"
    r"ON\s+CONFLICT\s*\(\s*(?P<cols>[^)]*?)\s*\)\s*"
    r"(?P<resolution>DO\s+NOTHING|DO\s+UPDATE)",
    re.IGNORECASE,
)


# ------------------------------------------------------------------------
# Shared AST helpers (self-contained -- deliberately not imported from
# test_storage_backend_hardening.py; each meta-test file in this repo
# stands alone so a fix to one checker can't accidentally change another).
# ------------------------------------------------------------------------


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _joined_static_text(node: ast.AST) -> str:
    """Concatenate every statically-known string literal fragment
    reachable from ``node`` -- plain ``Constant`` strings, the literal
    text segments of an f-string (``JoinedStr``, skipping the
    interpolated ``{expr}`` parts, whose runtime value can't be known
    statically -- e.g. ``{self._schema}`` simply contributes nothing,
    so ``f"INSERT INTO {self._schema}.foo "`` joins to
    ``"INSERT INTO .foo "``), and ``+``-concatenation chains of the
    above.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _joined_static_text(node.left) + _joined_static_text(node.right)
    return ""


def _root_string_literal_nodes(tree: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    """Every top-of-a-concatenation-chain string-literal expression in
    the module -- a ``Constant`` string, ``JoinedStr``, or ``+`` chain of
    those, whose immediate parent is NOT itself one of those three
    shapes. Excludes a ``JoinedStr``'s own interpolated-fragment
    ``Constant`` children (those are pieces of ONE statement, not
    independent statements) and non-top operands of a ``+`` chain.
    """
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        is_str_const = isinstance(node, ast.Constant) and isinstance(node.value, str)
        is_joined = isinstance(node, ast.JoinedStr)
        is_add_binop = isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
        if not (is_str_const or is_joined or is_add_binop):
            continue
        parent = parents.get(node)
        parent_is_str_tree = isinstance(parent, ast.JoinedStr) or (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add))
        if parent_is_str_tree:
            continue
        roots.append(node)
    return roots


def _has_nearby_explanation(src_lines: list[str], lineno: int, end_lineno: int, window: int = 2) -> bool:
    """A statement built from adjacent string-literal fragments can have
    genuine Python ``#`` comments interspersed BETWEEN the fragments --
    that's exactly how ``record_call()``'s big explanatory comment block
    sits between its ``ON CONFLICT (...)`` and ``DO UPDATE SET ...``
    literal pieces in postgres.py, and it's still inside the resulting
    node's ``[lineno, end_lineno]`` span since the comment is physically
    between the first and last token of the same concatenated
    expression. Only standalone comment lines count (a trailing
    ``# nosec B608`` annotation on a code line does not, by design --
    it's not an explanation of the upsert choice).
    """
    lo = max(1, lineno - window)
    hi = min(len(src_lines), end_lineno + window)
    for i in range(lo, hi + 1):
        stripped = src_lines[i - 1].strip()
        if not stripped.startswith("#"):
            continue
        lowered = stripped.lower()
        if any(marker in lowered for marker in _EXPLANATION_MARKERS):
            return True
    return False


def _looks_append_only(table: str, cols: tuple[str, ...]) -> bool:
    """Heuristic: a single conflict column literally named ``hash``/
    ``*_hash`` means the row is keyed by a content hash -- the value for
    a given key never changes, so DO NOTHING is the CORRECT (not merely
    tolerable) resolution, never a bug. A table name ending in a
    log/history/event-ish suffix gets the same benefit of the doubt.
    """
    if len(cols) == 1:
        col = cols[0].lower()
        if col == "hash" or col.endswith("_hash"):
            return True
    return any(table.lower().endswith(hint) for hint in _APPEND_ONLY_TABLE_HINTS)


# ------------------------------------------------------------------------
# Extraction + grouping
# ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Stmt:
    table: str
    cols: tuple[str, ...]
    resolution: str  # "NOTHING" or "UPDATE"
    lineno: int
    explained: bool


@dataclass(frozen=True)
class OnConflictFinding:
    file: str
    line: int
    table: str
    peer_table: str
    arity: int
    detail: str

    @property
    def key(self) -> str:
        return f"on_conflict_asymmetry::{self.file}:{self.line}"


def _extract_statements(tree: ast.AST, parents: dict[ast.AST, ast.AST], src_lines: list[str]) -> list[_Stmt]:
    out: list[_Stmt] = []
    for node in _root_string_literal_nodes(tree, parents):
        text = _joined_static_text(node)
        upper = text.upper()
        if "INSERT" not in upper or "ON CONFLICT" not in upper:
            continue
        for m in _INSERT_ON_CONFLICT_RE.finditer(text):
            cols = tuple(c.strip() for c in m.group("cols").split(",") if c.strip())
            resolution = "NOTHING" if "NOTHING" in m.group("resolution").upper() else "UPDATE"
            lineno = getattr(node, "lineno", 0)
            end_lineno = getattr(node, "end_lineno", lineno)
            out.append(
                _Stmt(
                    table=m.group("table"),
                    cols=cols,
                    resolution=resolution,
                    lineno=lineno,
                    explained=_has_nearby_explanation(src_lines, lineno, end_lineno),
                ),
            )
    return out


def scan_on_conflict_consistency(path: Path) -> list[OnConflictFinding]:
    """Regex-extract every INSERT ... ON CONFLICT statement in ``path``
    and flag a ``DO NOTHING`` statement that disagrees with ANY
    ``DO UPDATE`` statement elsewhere in the same file (both presumed
    per-attempt records, per ``_looks_append_only``) with no nearby
    explanatory comment. Conflict-column arity is NOT part of the
    gating predicate -- it's reported per-statement as descriptive
    metadata only -- because the historical bug this scanner
    generalizes from (``record_call()`` vs ``persist_winners()``)
    involved statements with different arities (4 vs 2); gating on
    arity equality would silently miss that shape of bug (and any
    future recurrence of it). Every finding is advisory -- see the
    module docstring.
    """
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [
            OnConflictFinding(
                file=path.name, line=0, table="", peer_table="", arity=0,
                detail=f"{path.name}: SyntaxError: {e}",
            ),
        ]

    src_lines = src.splitlines()
    parents = _parent_map(tree)
    stmts = _extract_statements(tree, parents, src_lines)
    per_attempt = [s for s in stmts if not _looks_append_only(s.table, s.cols)]

    resolutions = {s.resolution for s in per_attempt}
    if len(resolutions) < 2:
        return []  # every per-attempt statement in this file agrees -- nothing to flag

    do_update = [s for s in per_attempt if s.resolution == "UPDATE"]
    # Guaranteed non-empty: `resolutions` has exactly 2 members here
    # ("NOTHING" and "UPDATE"), so at least one DO UPDATE statement exists.
    peer = do_update[0]

    findings: list[OnConflictFinding] = []
    for s in per_attempt:
        if s.resolution != "NOTHING" or s.explained:
            continue
        findings.append(
            OnConflictFinding(
                file=path.name, line=s.lineno, table=s.table, peer_table=peer.table, arity=len(s.cols),
                detail=(
                    f"{path.name}:{s.lineno}: INSERT INTO {s.table} ON CONFLICT "
                    f"({', '.join(s.cols)}, arity {len(s.cols)}) DO NOTHING, while "
                    f"{peer.table} in the same file (arity {len(peer.cols)}) resolves "
                    f"ON CONFLICT with DO UPDATE instead. If both represent a "
                    f"per-attempt record (not a clearly append-only/immutable log), a "
                    f"later successful retry into {s.table} after a prior failed "
                    f"attempt may be silently dropped -- regardless of the two "
                    f"statements' conflict-column arity matching or not. Advisory: if "
                    f"the difference is deliberate, add a short comment near the ON "
                    f"CONFLICT clause explaining why -- this also silences the scanner "
                    f"directly -- or review and refresh the baseline (see this file's "
                    f"module docstring)."
                ),
            ),
        )
    return findings


# ------------------------------------------------------------------------
# Baseline-ratchet test against the real storage backends
# ------------------------------------------------------------------------


def _refresh_requested() -> bool:
    return _REFRESH_FLAG in sys.argv


def test_no_new_on_conflict_asymmetries():
    all_findings: list[OnConflictFinding] = []
    for path in sorted(_STORAGE_ROOT.glob("*.py")):
        all_findings.extend(scan_on_conflict_consistency(path))
    current_by_key = {f.key: f for f in all_findings}
    current_keys = set(current_by_key)

    if _refresh_requested() or not _BASELINE_PATH.exists():
        _BASELINE_PATH.write_text(
            orjson.dumps(sorted(current_keys), option=orjson.OPT_INDENT_2).decode("utf-8"),
            encoding="utf-8",
        )
        pytest.skip(f"SQL on-conflict baseline refreshed at {_BASELINE_PATH.name} " f"({len(current_keys)} existing finding(s))")

    baseline = set(orjson.loads(_BASELINE_PATH.read_bytes()))
    new = sorted(current_keys - baseline)
    fixed = sorted(baseline - current_keys)

    if fixed:
        sys.stderr.write(
            f"\n[test_no_new_on_conflict_asymmetries] {len(fixed)} finding(s) DRAINED "
            f"(no longer reproduce -- baseline is stale, refresh it):\n  " + "\n  ".join(fixed) + "\n"
        )

    if new:
        detail_lines = [current_by_key[k].detail for k in new]
        pytest.fail(
            f"{len(new)} new asymmetric ON CONFLICT finding(s) -- advisory, needs a "
            f"one-line human sign-off:\n  " + "\n  ".join(detail_lines) + "\n  Either add a "
            f"comment near the ON CONFLICT clause explaining the deliberate difference "
            f"(silences the scanner directly), or review and refresh: "
            f"pytest tests/test_meta/test_sql_on_conflict_consistency.py {_REFRESH_FLAG}"
        )


# ------------------------------------------------------------------------
# Regression tests for the checker itself (synthetic source, tmp_path)
# ------------------------------------------------------------------------


class TestOnConflictConsistencyDetectsShapes:
    def _run(self, tmp_path, source: str) -> list[OnConflictFinding]:
        p = tmp_path / "synthetic_postgres.py"
        p.write_text(source, encoding="utf-8")
        return scan_on_conflict_consistency(p)

    def test_asymmetric_do_nothing_fires(self, tmp_path):
        source = (
            "class Storage:\n"
            "    async def record_call(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_attempts (a, b) VALUES ($1, $2) '\n"
            "                f'ON CONFLICT (a, b) DO NOTHING',\n"
            "                row.a, row.b,\n"
            "            )\n"
            "    async def persist_winners(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_winners (a, b) VALUES ($1, $2) '\n"
            "                f'ON CONFLICT (a, b) DO UPDATE SET a=EXCLUDED.a',\n"
            "                row.a, row.b,\n"
            "            )\n"
        )
        findings = self._run(tmp_path, source)
        assert len(findings) == 1
        assert findings[0].table == "benchmark_attempts"
        assert findings[0].peer_table == "benchmark_winners"

    def test_explained_asymmetry_is_silent(self, tmp_path):
        source = (
            "class Storage:\n"
            "    async def record_call(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_attempts (a, b) VALUES ($1, $2) '\n"
            "                # Deliberately DO NOTHING here: unlike benchmark_winners, a\n"
            "                # conflicting attempt row for benchmark_attempts is always a\n"
            "                # verbatim duplicate, so DO UPDATE would be a no-op anyway.\n"
            "                f'ON CONFLICT (a, b) DO NOTHING',\n"
            "                row.a, row.b,\n"
            "            )\n"
            "    async def persist_winners(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_winners (a, b) VALUES ($1, $2) '\n"
            "                f'ON CONFLICT (a, b) DO UPDATE SET a=EXCLUDED.a',\n"
            "                row.a, row.b,\n"
            "            )\n"
        )
        findings = self._run(tmp_path, source)
        assert findings == []

    def test_different_arity_is_still_compared(self, tmp_path):
        # This reconstructs the actual historical bug shape: record_call()'s
        # 4-column-equivalent ON CONFLICT (here, 2 cols) DO NOTHING vs
        # persist_winners()'s differently-shaped (1 col) DO UPDATE, in the
        # same file. Arity must NOT gate the comparison -- the scanner has
        # to catch this regardless of the two statements' column counts
        # matching, since that's precisely the shape it exists to detect.
        source = (
            "class Storage:\n"
            "    async def record_call(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_attempts (a, b) VALUES ($1, $2) '\n"
            "                f'ON CONFLICT (a, b) DO NOTHING',\n"
            "                row.a, row.b,\n"
            "            )\n"
            "    async def persist_winners(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_winners (c) VALUES ($1) '\n"
            "                f'ON CONFLICT (c) DO UPDATE SET c=EXCLUDED.c',\n"
            "                row.c,\n"
            "            )\n"
        )
        findings = self._run(tmp_path, source)
        assert len(findings) == 1
        assert findings[0].table == "benchmark_attempts"
        assert findings[0].peer_table == "benchmark_winners"
        assert findings[0].arity == 2

    def test_hash_keyed_append_only_table_is_exempt(self, tmp_path):
        # Single conflict column named "hash" is content-addressed dedup
        # (the value for a given key never changes) -- DO NOTHING there
        # is correct, so it must never be compared against a same-arity
        # per-attempt table even when that table uses DO UPDATE.
        source = (
            "class Storage:\n"
            "    async def upsert_prompts(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_user_prompts (hash) VALUES ($1) '\n"
            "                f'ON CONFLICT (hash) DO NOTHING',\n"
            "                row.hash,\n"
            "            )\n"
            "    async def bump_counter(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_counters (id) VALUES ($1) '\n"
            "                f'ON CONFLICT (id) DO UPDATE SET n=EXCLUDED.n',\n"
            "                row.id,\n"
            "            )\n"
        )
        findings = self._run(tmp_path, source)
        assert findings == []

    def test_all_do_nothing_at_same_arity_is_silent(self, tmp_path):
        source = (
            "class Storage:\n"
            "    async def a(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_attempts (a) VALUES ($1) '\n"
            "                f'ON CONFLICT (a) DO NOTHING',\n"
            "                row.a,\n"
            "            )\n"
            "    async def b(self, row):\n"
            "        async with pool.acquire() as conn:\n"
            "            await conn.execute(\n"
            "                f'INSERT INTO {self._schema}.benchmark_units (b) VALUES ($1) '\n"
            "                f'ON CONFLICT (b) DO NOTHING',\n"
            "                row.b,\n"
            "            )\n"
        )
        findings = self._run(tmp_path, source)
        assert findings == []

    def test_syntax_error_reported(self, tmp_path):
        p = tmp_path / "broken.py"
        p.write_text("def f(:\n    pass\n", encoding="utf-8")
        findings = scan_on_conflict_consistency(p)
        assert len(findings) == 1
        assert "SyntaxError" in findings[0].detail
