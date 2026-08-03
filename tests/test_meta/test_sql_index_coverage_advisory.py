"""Advisory scanner: approximate SQL index-vs-query column coverage check
for ``storage/postgres.py``'s DDL and SQL text.

Motivation (see ``test_storage_backend_hardening.py`` for the settled
production-fix state this complements): ``prefetch_resume_cache()``'s WHERE
clause (``response IS NOT NULL AND length(response) >= $1 AND error_class IS
NULL``) originally had ZERO supporting index among the three that existed at
the time, causing a full sequential scan of ``benchmark_results`` on every
process start; a single-column ``stage`` index existed even though ``stage``
always co-occurred with ``experiment_tag`` in every real query in this file;
and one index (``idx_benchmark_results_task_unit``) is referenced by no
query in this file at all.

*** THIS CHECK IS ADVISORY ONLY -- IT IS NOT A REAL QUERY PLANNER ***

It regex-extracts ``CREATE INDEX ... ON <table> (<cols>) [WHERE <pred>]``
and ``SELECT ... FROM <table> WHERE <cond>`` text out of postgres.py's
string literals, then does a crude column-NAME co-occurrence check. It
cannot see:

  - table PRIMARY KEY constraints -- Postgres auto-creates a unique index
    for every PRIMARY KEY, so a WHERE clause matching a PK's columns is
    really covered even though no CREATE INDEX statement declares it
    (``load_winners()`` hits exactly this -- see ``_WHITELIST`` below).
  - a real planner's ability to combine multiple single-column indexes
    (Bitmap AND/OR), use a partial index whose predicate merely IMPLIES the
    query's WHERE (rather than textually containing the same column
    names), or exploit column ordering / prefix matching within a
    composite index.
  - SQL assembled across multiple Python statements (e.g. a conditional
    ``sql += "..."`` appended after the initial assignment) -- each string
    literal is analyzed as its own independent text blob, so a WHERE
    fragment appended later is invisible to this scanner. This is not
    hypothetical: ``query_rows()`` and ``prefetch_resume_cache()`` both
    build their WHERE clause with exactly this ``sql += "..."`` pattern,
    so a future edit that moves the coverage-critical column into the
    appended fragment (rather than the initial assignment) would escape
    detection silently.
  - non-SELECT statements -- ``_SELECT_WHERE_RE`` requires a literal
    ``SELECT`` keyword, so ``DELETE ... WHERE ...`` clauses (e.g.
    ``delete_experiment()``'s two ``DELETE`` statements) are never scanned
    at all, not even as an uncovered-WHERE finding.

Given those blind spots, this is a textual pattern-matching heuristic, not
an authoritative correctness check. The test below NEVER fails the suite --
it prints a report and, if there are any un-whitelisted findings, raises a
non-fatal ``UserWarning`` (visible in pytest's warnings summary) so a human
can triage them. A confirmed non-issue gets a one-line comment in
``_WHITELIST`` rather than being silently dropped from future reports.
"""

from __future__ import annotations

import ast
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

_STORAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench" / "storage"
_POSTGRES_PATH = _STORAGE_ROOT / "postgres.py"

# Per-finding whitelist: entries trimmed from the printed/warned report
# because they're confirmed non-issues this textual scanner cannot model
# (see the module docstring's "cannot see" list above).
_WHITELIST: set[str] = {
    # idx_benchmark_results_task_unit is deliberately kept for an external
    # psql/BI operator use case even though no query in this file filters
    # on task_unit_id alone -- see the comment directly above its
    # CREATE INDEX in postgres.py (audit: 08-Medium). Confirmed non-bug.
    "dead_index::idx_benchmark_results_task_unit",
    # load_winners() filters benchmark_winners on its exact PRIMARY KEY
    # (experiment_tag, round_idx). Postgres auto-creates a unique index for
    # every PRIMARY KEY, which this scanner cannot see (it only looks at
    # explicit CREATE INDEX statements) -- confirmed non-bug, not a gap.
    "uncovered_where::load_winners",
}

# ------------------------------------------------------------------------
# Regexes
# ------------------------------------------------------------------------

# An identifier immediately followed by a comparison operator (or `IS`) is
# treated as a "referenced column" -- deliberately crude so it skips
# function names like `length(` (followed by `(`, not an operator).
_COL_TOKEN_RE = re.compile(
    r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b\s*(?:=(?!=)|>=|<=|<>|!=|>|<|IS\b)",
    re.IGNORECASE,
)

_CREATE_INDEX_RE = re.compile(
    r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>\w+)\s+" r"ON\s+(?P<tablepath>[^\s(]+)\s*\(\s*(?P<cols>[^)]*)\)" r"(?:\s+WHERE\s+(?P<pred>.*?);)?",
    re.IGNORECASE | re.DOTALL,
)

_SELECT_WHERE_RE = re.compile(
    r"\bSELECT\b.*?\bFROM\s+(?P<tablepath>[^\s(]+)\s+WHERE\s+" r"(?P<cond>.+?)(?=;|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\Z)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class _IndexDef:
    name: str
    table: str
    cols: frozenset[str]
    lineno: int


@dataclass(frozen=True)
class _QueryWhere:
    caller: str  # nearest enclosing function name, for reporting/whitelisting
    table: str
    cols: frozenset[str]
    lineno: int


# ------------------------------------------------------------------------
# AST helpers (self-contained -- no cross-import from sibling meta-tests)
# ------------------------------------------------------------------------


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _joined_static_text(node: ast.expr) -> str:
    """Concatenate every statically-known string literal fragment reachable
    from ``node`` -- plain ``Constant`` strings and the literal text
    segments of an f-string (``JoinedStr``, skipping the interpolated
    ``{expr}`` parts, whose runtime value can't be known statically)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
    return ""


def _enclosing_function_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    cur: ast.AST | None = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parents.get(cur)
    return "<module>"


def _top_level_string_blobs(tree: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[tuple[str, int, str]]:
    """Every top-level string-literal expression in the file (a plain
    ``Constant`` string not nested inside a ``JoinedStr``, or a whole
    ``JoinedStr``), as ``(joined_static_text, lineno, enclosing_func_name)``.
    Constants that are literal segments OF a JoinedStr are skipped here --
    they're already folded into that JoinedStr's joined text -- so each
    logical string literal in the source is represented exactly once."""
    blobs: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            if isinstance(parents.get(node), ast.JoinedStr):
                continue
            blobs.append((_joined_static_text(node), node.lineno, _enclosing_function_name(node, parents)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if isinstance(parents.get(node), ast.JoinedStr):
                continue
            blobs.append((node.value, node.lineno, _enclosing_function_name(node, parents)))
    return blobs


# ------------------------------------------------------------------------
# Column extraction
# ------------------------------------------------------------------------


def _table_name(tablepath: str) -> str:
    """``{s}.benchmark_results`` becomes ``.benchmark_results`` once the
    dynamic ``{s}`` part is skipped by ``_joined_static_text`` -- strip
    down to the bare table name after the last dot (or the whole thing if
    there's no dot at all)."""
    tablepath = tablepath.strip().rstrip(",;")
    return tablepath.rsplit(".", 1)[-1].strip() or tablepath


def _index_cols(cols_text: str) -> frozenset[str]:
    out: set[str] = set()
    for part in cols_text.split(","):
        part = part.strip()
        if not part:
            continue
        first_tok = re.split(r"\s+", part)[0].strip("()").lower()
        if first_tok:
            out.add(first_tok)
    return frozenset(out)


def _cond_cols(cond_text: str) -> frozenset[str]:
    return frozenset(m.lower() for m in _COL_TOKEN_RE.findall(cond_text))


def _extract_indexes(blobs: list[tuple[str, int, str]]) -> list[_IndexDef]:
    out: list[_IndexDef] = []
    for text, lineno, _caller in blobs:
        for m in _CREATE_INDEX_RE.finditer(text):
            table = _table_name(m.group("tablepath"))
            cols = set(_index_cols(m.group("cols")))
            pred = m.group("pred")
            if pred:
                # Partial-index predicate columns genuinely affect whether
                # Postgres can use the index for a given query, so fold
                # them into the index's "coverage" column set too.
                cols |= _cond_cols(pred)
            out.append(_IndexDef(name=m.group("name"), table=table, cols=frozenset(cols), lineno=lineno))
    return out


def _extract_queries(blobs: list[tuple[str, int, str]]) -> list[_QueryWhere]:
    out: list[_QueryWhere] = []
    for text, lineno, caller in blobs:
        for m in _SELECT_WHERE_RE.finditer(text):
            table = _table_name(m.group("tablepath"))
            cols = _cond_cols(m.group("cond"))
            if cols:
                out.append(_QueryWhere(caller=caller, table=table, cols=cols, lineno=lineno))
    return out


# ------------------------------------------------------------------------
# The scanner
# ------------------------------------------------------------------------


def scan_sql_index_coverage(path: Path) -> list[str]:
    """Advisory-only findings (see module docstring). Returns a list of
    human-readable strings; empty means "nothing (un-whitelisted) to
    report", not "provably correct"."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        return [f"{path.name}: SyntaxError: {e}"]

    parents = _parent_map(tree)
    blobs = _top_level_string_blobs(tree, parents)
    indexes = _extract_indexes(blobs)
    queries = _extract_queries(blobs)

    findings: list[str] = []

    # (a) possibly-dead index: none of its columns co-occur in any WHERE
    # clause found for the same table anywhere in the file.
    for idx in indexes:
        same_table_query_cols: set[str] = set()
        for q in queries:
            if q.table == idx.table:
                same_table_query_cols |= q.cols
        if idx.cols and not (idx.cols & same_table_query_cols):
            key = f"dead_index::{idx.name}"
            if key in _WHITELIST:
                continue
            findings.append(
                f"{path.name}:{idx.lineno}: [possibly-dead-index] {idx.name} "
                f"ON {idx.table} ({', '.join(sorted(idx.cols))}) -- none of "
                f"its columns appear in any WHERE clause found in this "
                f"file. Advisory only (textual match, not a real planner) "
                f"-- verify with pg_stat_user_indexes before dropping."
            )

    # (b) WHERE clause with no even-partially-covering index on the same
    # table (column-set intersection, however small, counts as coverage).
    for q in queries:
        same_table_indexes = [idx for idx in indexes if idx.table == q.table]
        covering = any(idx.cols & q.cols for idx in same_table_indexes)
        if not covering:
            key = f"uncovered_where::{q.caller}"
            if key in _WHITELIST:
                continue
            findings.append(
                f"{path.name}:{q.lineno}: [uncovered-where-clause] "
                f"{q.caller}()'s WHERE on {q.table} "
                f"({', '.join(sorted(q.cols))}) has no even-partially-"
                f"covering CREATE INDEX found in this file. Advisory only "
                f"-- may be covered by a PRIMARY KEY or a real planner "
                f"capability this textual check can't model."
            )
    return findings


# ------------------------------------------------------------------------
# Soft, non-blocking report against the real file
# ------------------------------------------------------------------------


def test_sql_index_coverage_advisory_report():
    """ADVISORY ONLY -- see the module docstring for the full list of
    blind spots (no PRIMARY KEY awareness, no real planner, no cross-
    statement SQL assembly). This test intentionally NEVER fails the
    suite: it always passes, printing any un-whitelisted findings and
    raising a non-fatal UserWarning (shown in pytest's warnings summary)
    so a human can triage them. Do not add a hard pytest.fail() here --
    the textual approximation this scanner uses has real, expected false
    positives (tracked via _WHITELIST) and false negatives, so it must
    never gate CI."""
    findings = scan_sql_index_coverage(_POSTGRES_PATH)
    if findings:
        report = "\n  ".join(findings)
        print(  # noqa: T201 -- intentional advisory report, see module docstring
            f"\n[sql-index-coverage-advisory] {len(findings)} finding(s) " f"in postgres.py (non-blocking, informational only):\n  {report}"
        )
        warnings.warn(
            f"sql-index-coverage-advisory: {len(findings)} informational "
            f"finding(s) in postgres.py -- rerun with -s to see the "
            f"report, or see test_sql_index_coverage_advisory.py. This "
            f"does not fail the suite.",
            stacklevel=1,
        )


# ------------------------------------------------------------------------
# Regression tests for the checker itself (synthetic source, tmp_path)
# ------------------------------------------------------------------------


class TestSqlIndexCoverageDetectsShapes:
    def _run(self, tmp_path, source: str) -> list[str]:
        p = tmp_path / "synthetic_postgres.py"
        p.write_text(source, encoding="utf-8")
        return scan_sql_index_coverage(p)

    def test_dead_index_fires(self, tmp_path):
        # idx_used is referenced by query_rows()'s WHERE; idx_dead never
        # co-occurs with any WHERE clause in the file -- only the latter
        # should fire.
        source = (
            "def _ddl(schema):\n"
            "    s = schema\n"
            "    return [\n"
            "        f'CREATE INDEX IF NOT EXISTS idx_used ON {s}.mytable (used_col);',\n"
            "        f'CREATE INDEX IF NOT EXISTS idx_dead ON {s}.mytable (dead_col);',\n"
            "    ]\n"
            "async def query_rows(self):\n"
            "    sql = f'SELECT * FROM {self._schema}.mytable WHERE used_col=$1'\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "possibly-dead-index" in violations[0]
        assert "idx_dead" in violations[0]

    def test_uncovered_where_fires(self, tmp_path):
        # No CREATE INDEX anywhere in the file -- the WHERE clause below
        # has nothing to even partially cover it.
        source = "async def load_thing(self):\n    sql = f'SELECT x FROM {self._schema}.mytable WHERE other_col=$1'\n"
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "uncovered-where-clause" in violations[0]
        assert "load_thing" in violations[0]

    def test_covered_and_used_index_is_silent(self, tmp_path):
        source = (
            "def _ddl(schema):\n"
            "    s = schema\n"
            "    return [f'CREATE INDEX IF NOT EXISTS idx_tag ON {s}.mytable (tag);']\n"
            "async def query_rows(self):\n"
            "    sql = f'SELECT * FROM {self._schema}.mytable WHERE tag=$1'\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []

    def test_partial_index_predicate_columns_count_as_coverage(self, tmp_path):
        # The index's own paren column list (pk1, pk2) doesn't textually
        # match the query's WHERE (on `response`), but the partial
        # index's WHERE predicate does -- this must be treated as
        # covering, mirroring idx_benchmark_results_resume_cache's real
        # shape in postgres.py.
        source = (
            "def _ddl(schema):\n"
            "    s = schema\n"
            "    return [\n"
            '        """\n'
            "        CREATE INDEX IF NOT EXISTS idx_partial\n"
            "            ON mytable (pk1, pk2)\n"
            "            WHERE response IS NOT NULL;\n"
            '        """,\n'
            "    ]\n"
            "async def prefetch(self):\n"
            "    sql = f'SELECT pk1 FROM {self._schema}.mytable WHERE response IS NOT NULL'\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []

    def test_syntax_error_reported(self, tmp_path):
        violations = self._run(tmp_path, "def broken(:\n")
        assert len(violations) == 1
        assert "SyntaxError" in violations[0]
