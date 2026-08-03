"""Ban inspect.getsource()/read_text() used as behavioral-proxy test assertions.

Memory rule (feedback_behavioral_tests): tests must assert runtime BEHAVIOUR,
never inspect source text as a stand-in for it -- e.g.
``assert "foo" in inspect.getsource(func)`` or
``some_path.read_text().find("bar") > 0`` used to claim a function does X,
instead of actually calling it and checking the result. Ported from
mlframe's tests/test_meta/test_no_inspect_getsource.py (its two checks --
this file keeps scan + tests + regression class self-contained in one
module, matching this repo's own meta-test convention rather than mlframe's
separate ``_source_proxy_scan.py`` helper module).

Two independent, hard-failing checks (an inline ``_WHITELIST`` per check is
the escape hatch, matching test_no_unicode_in_console_output.py's
convention). Both are hard asserts, not baseline-ratcheted: the shapes below
were chosen specifically because a scan of the CURRENT tests/ tree turns up
zero real hits for either -- the wider shapes that DO have real, currently
legitimate uses in this repo are deliberately excluded from check 2 (see
its docstring for the two live examples that motivated narrowing it).

  1. ``test_no_inspect_getsource_proxy_assertions`` -- flags a name bound to
     ``inspect.getsource(...)`` (or bare ``getsource(...)``), OR a direct
     inline ``inspect.getsource(...)`` call, that feeds an ``in``/``not in``
     membership test, a ``.find()``/``.index()``/``.rfind()`` call, or a
     ``re.search()``/``re.match()``/``re.fullmatch()`` call -- the full
     shape list the task calls for. A legitimate structural/documentation
     check (e.g. "does this function have a docstring that claims X")
     instead uses ``ast.get_docstring()`` directly on the parsed node --
     see tests/test_meta/test_unwired_capabilities.py and
     tests/test_meta/test_protocol_async_optional_dispatch.py, both of
     which regex-search a docstring pulled via ``ast.get_docstring()``.
     That path never calls ``inspect.getsource`` at all, so it is naturally
     untouched by this scan -- no special-casing needed to keep it legal.

  2. ``test_no_read_text_position_proxy_assertions`` -- flags a name bound
     to ``<expr>.read_text(...)``, or a direct inline
     ``<expr>.read_text(...)`` call, that feeds ``.find()``/``.index()``/
     ``.rfind()`` (asserting on the BYTE POSITION of a substring inside
     arbitrary file text). Deliberately narrower than check 1: it does NOT
     flag ``"literal" in read_text_var`` membership or a
     ``re.search()``/``re.match()``/``re.fullmatch()`` call fed by
     read_text, because both shapes have real, currently-live, legitimate
     uses in this exact repo:
       - tests/unit/test_storage_protocol.py::test_upsert_prompts_actually_persists_text
         reads back a data artifact FileStorage itself just wrote, to
         verify the real persisted content -- the read_text() call IS the
         behavioural check here, not a proxy for one.
       - tests/integration/test_readme_quickstart.py and
         tests/test_meta/test_version_consistency.py regex-EXTRACT a value
         (a code block to actually exec, a version string to compare
         against the real ``llm_bench.__version__`` at runtime) rather than
         asserting source content as a stand-in for behaviour.
     The find/index/rfind byte-position pattern has no such use anywhere in
     this repo and is a strictly narrower, unambiguous proxy-for-behaviour
     shape -- ported from mlframe's
     ``test_no_source_text_position_proxy_in_test_files``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_TESTS = Path(__file__).resolve().parent.parent  # tests/

# Per-site whitelist for confirmed false positives: "<rel_path>::<lineno>".
_GETSOURCE_WHITELIST: set[str] = set()
_READ_TEXT_FIND_WHITELIST: set[str] = set()

_POSITION_METHODS = frozenset({"find", "index", "rfind"})
_REGEX_FUNCS = frozenset({"search", "match", "fullmatch"})


def _source_call_kind(node: ast.expr) -> str | None:
    """Return "getsource" if any sub-call of ``node`` is ``inspect.getsource(...)``
    (or bare ``getsource(...)``), else "read_text" if any sub-call is
    ``<expr>.read_text(...)``, else None. getsource takes priority since a
    single expression realistically only ever chains one of the two."""
    kind: str | None = None
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if (isinstance(func, ast.Attribute) and func.attr == "getsource") or (isinstance(func, ast.Name) and func.id == "getsource"):
            return "getsource"
        if isinstance(func, ast.Attribute) and func.attr == "read_text":
            kind = "read_text"
    return kind


def _root_name(node: ast.expr) -> str | None:
    """Return the base ``Name`` id of a (possibly chained) attribute/subscript
    expression, or None (e.g. for a bare Call, which is left un-resolved here --
    callers fall back to ``_source_call_kind`` for that shape)."""
    cur = node
    while isinstance(cur, (ast.Attribute, ast.Subscript)):
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id
    return None


def _resolved_kind(expr: ast.expr, names: dict[str, str]) -> str | None:
    """Kind of ``expr`` (see ``_source_call_kind``), whether it is a bare NAME
    already bound (in ``names``) to a source call, or the source call written
    directly inline with no intermediate variable (e.g.
    ``inspect.getsource(foo).find("x")`` or ``path.read_text().find("x")``)."""
    root = _root_name(expr)
    if root is not None and root in names:
        return names[root]
    return _source_call_kind(expr)


def _iter_scope_nodes(scope: ast.AST):
    """Yield statement nodes lexically inside ``scope`` WITHOUT descending into
    nested function/class definitions (those are their own scope). The initial
    seed from ``scope.body`` must be filtered the same way the recursive step
    filters children -- otherwise, when ``scope`` is the module and its body's
    direct children are top-level FunctionDefs, those defs get yielded (and
    their own bodies walked) as if they were module-scope statements, leaking
    every top-level function's local bindings into module scope and from there
    into every OTHER function's merged namespace."""
    stack = [n for n in getattr(scope, "body", []) if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(child)


def _scope_source_names(scope: ast.AST) -> dict[str, str]:
    """Names assigned from a getsource/read_text expression directly inside
    ``scope`` (not nested function/class bodies) -- mapped to "getsource" or
    "read_text". Scoping matters: a same-named var bound to something unrelated
    in another function must never be conflated with a source-call binding here."""
    names: dict[str, str] = {}
    for node in _iter_scope_nodes(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            value = node.value
            if value is None:
                continue
            kind = _source_call_kind(value)
            if kind is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names[t.id] = kind
    return names


def _check_compare_site(node: ast.Compare, names: dict[str, str]) -> tuple[int, str, str] | None:
    """Shape: "literal" in <getsource-result> / "literal" not in <getsource-result>.
    (read_text is deliberately NOT matched here -- see module docstring.)"""
    if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.In, ast.NotIn)):  # codespell:ignore
        return None
    left = node.left
    if not (isinstance(left, ast.Constant) and isinstance(left.value, (str, bytes))):
        return None
    if _resolved_kind(node.comparators[0], names) != "getsource":
        return None
    op = "in" if isinstance(node.ops[0], ast.In) else "not in"
    lit = left.value if isinstance(left.value, str) else left.value.decode("latin-1", "replace")
    return (node.lineno, "getsource", f'"{lit}" {op} <getsource-result>')


def _check_position_call(func: ast.Attribute, node: ast.Call, names: dict[str, str]) -> tuple[int, str, str] | None:
    """Shape: <getsource-or-read_text-result>.find/index/rfind(...). Both kinds match."""
    kind = _resolved_kind(func.value, names)
    if kind is None:
        return None
    return (node.lineno, kind, f"<{kind}-result>.{func.attr}(...)")


def _check_regex_call(func: ast.Attribute, node: ast.Call, names: dict[str, str]) -> tuple[int, str, str] | None:
    """Shape: re.search/match/fullmatch(pattern, <getsource-result>).
    (read_text is deliberately NOT matched here -- see module docstring.)"""
    str_arg = node.args[1] if len(node.args) > 1 else next((kw.value for kw in node.keywords if kw.arg == "string"), None)
    if str_arg is None or _resolved_kind(str_arg, names) != "getsource":
        return None
    return (node.lineno, "getsource", f"re.{func.attr}(pattern, <getsource-result>)")


def _check_call_site(node: ast.Call, names: dict[str, str]) -> tuple[int, str, str] | None:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in _POSITION_METHODS:
        return _check_position_call(func, node, names)
    if func.attr in _REGEX_FUNCS and isinstance(func.value, ast.Name) and func.value.id == "re":
        return _check_regex_call(func, node, names)
    return None


def _find_proxy_sites(path: Path) -> list[tuple[int, str, str]]:
    """Return [(lineno, kind, shape)] for each behavioral-proxy assertion
    shape applied to a getsource()/read_text()-derived string, where kind is
    "getsource" or "read_text". Pure function -- reads ``path`` from disk,
    does not touch pytest state, so it is directly unit-testable."""
    try:
        src = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []

    module_names = _scope_source_names(tree)
    hits: list[tuple[int, str, str]] = []
    seen: set[int] = set()

    func_scopes: list[ast.AST] = [tree]
    func_scopes.extend(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))

    for scope in func_scopes:
        names = {**module_names, **_scope_source_names(scope)}
        for node in _iter_scope_nodes(scope):
            if id(node) in seen:
                continue
            hit: tuple[int, str, str] | None = None
            if isinstance(node, ast.Compare):
                hit = _check_compare_site(node, names)
            elif isinstance(node, ast.Call):
                hit = _check_call_site(node, names)
            if hit is not None:
                seen.add(id(node))
                hits.append(hit)

    hits.sort()
    return hits


def _iter_test_files() -> list[Path]:
    """All ``*.py`` files under tests/ (skips __pycache__)."""
    return [p for p in _REPO_TESTS.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_inspect_getsource_proxy_assertions():
    """Check 1 -- see module docstring. inspect.getsource() feeding an
    in/not-in/.find()/.index()/.rfind()/regex-search assertion violates
    feedback_behavioral_tests: call the real function and assert its result."""
    offenders: list[str] = []
    for path in _iter_test_files():
        rel = path.relative_to(_REPO_TESTS).as_posix()
        for lineno, kind, shape in _find_proxy_sites(path):
            if kind != "getsource":
                continue
            key = f"{rel}::{lineno}"
            if key in _GETSOURCE_WHITELIST:
                continue
            offenders.append(f"{rel}:{lineno}: inspect.getsource() feeds {shape}")
    if offenders:
        msg = "\n  ".join(offenders)
        pytest.fail(
            f"inspect.getsource() used as a behaviour proxy:\n  {msg}\n"
            f"Replace with a real runtime check (call the function, assert on its "
            f"return value / raised exception / side effect), or if this is a "
            f"confirmed structural check (e.g. call-order in an uncompilable-without-"
            f"hardware code path), add the site to _GETSOURCE_WHITELIST with a reason."
        )


def test_no_read_text_position_proxy_assertions():
    """Check 2 -- see module docstring. read_text() feeding a
    .find()/.index()/.rfind() byte-position assertion violates
    feedback_behavioral_tests: it asserts WHERE a substring sits in raw text
    instead of asserting runtime behaviour."""
    offenders: list[str] = []
    for path in _iter_test_files():
        rel = path.relative_to(_REPO_TESTS).as_posix()
        for lineno, kind, shape in _find_proxy_sites(path):
            if kind != "read_text":
                continue
            key = f"{rel}::{lineno}"
            if key in _READ_TEXT_FIND_WHITELIST:
                continue
            offenders.append(f"{rel}:{lineno}: read_text() feeds {shape}")
    if offenders:
        msg = "\n  ".join(offenders)
        pytest.fail(
            f"read_text() result used as a source-text-position proxy:\n  {msg}\n"
            f"Replace with a real runtime/structural check, or if this is a confirmed "
            f"false positive, add the site to _READ_TEXT_FIND_WHITELIST with a reason."
        )


class TestFindProxySitesDetectsShapes:
    """Direct regression tests for ``_find_proxy_sites`` itself, against
    synthetic source written to a real temp file (matches
    test_no_unicode_in_console_output.py's ``_audit_file`` convention: the
    scanner reads from disk, not from a string)."""

    def _run(self, tmp_path: Path, source: str) -> list[tuple[int, str, str]]:
        p = tmp_path / "synthetic.py"
        p.write_text(source, encoding="utf-8")
        return _find_proxy_sites(p)

    # --- getsource shapes: all fire ---------------------------------

    def test_getsource_membership_in_detected(self, tmp_path):
        hits = self._run(
            tmp_path,
            "import inspect\n" "def f(func):\n" "    src = inspect.getsource(func)\n" '    assert "def helper" in src\n',
        )
        assert len(hits) == 1
        assert hits[0][1] == "getsource"

    def test_getsource_membership_not_in_detected(self, tmp_path):
        hits = self._run(
            tmp_path,
            "import inspect\n" "def f(func):\n" "    src = inspect.getsource(func)\n" '    assert "TODO" not in src\n',
        )
        assert len(hits) == 1
        assert hits[0][1] == "getsource"

    def test_getsource_find_detected(self, tmp_path):
        hits = self._run(
            tmp_path,
            "import inspect\n" "def f(func):\n" "    src = inspect.getsource(func)\n" '    assert src.find("return") > 0\n',
        )
        assert len(hits) == 1
        assert "find" in hits[0][2]

    def test_getsource_index_detected(self, tmp_path):
        hits = self._run(
            tmp_path,
            "import inspect\n" "def f(func):\n" "    src = inspect.getsource(func)\n" '    assert src.index("return") > 0\n',
        )
        assert len(hits) == 1

    def test_getsource_rfind_detected(self, tmp_path):
        hits = self._run(
            tmp_path,
            "import inspect\n" "def f(func):\n" "    src = inspect.getsource(func)\n" '    assert src.rfind("return") > 0\n',
        )
        assert len(hits) == 1

    def test_getsource_regex_search_detected(self, tmp_path):
        hits = self._run(
            tmp_path,
            "import re\n" "import inspect\n" "def f(func):\n" "    src = inspect.getsource(func)\n" '    assert re.search(r"self\\.a\\(\\)", src) is not None\n',
        )
        assert len(hits) == 1
        assert hits[0][1] == "getsource"

    def test_getsource_bare_name_import_detected(self, tmp_path):
        hits = self._run(
            tmp_path,
            "from inspect import getsource\n" "def f(func):\n" "    src = getsource(func)\n" '    assert "x" in src\n',
        )
        assert len(hits) == 1

    def test_getsource_direct_inline_call_detected(self, tmp_path):
        # No intermediate variable at all -- the getsource() call is chained
        # straight into the assertion.
        hits = self._run(
            tmp_path,
            "import inspect\n" "def f(func):\n" '    assert "x" in inspect.getsource(func)\n',
        )
        assert len(hits) == 1
        assert hits[0][1] == "getsource"

    # --- read_text shapes: find/index/rfind fire, membership/regex do not ---

    def test_read_text_find_detected(self, tmp_path):
        hits = self._run(
            tmp_path,
            "from pathlib import Path\n" "def f(p):\n" "    text = p.read_text()\n" '    assert text.find("token") > 10\n',
        )
        assert len(hits) == 1
        assert hits[0][1] == "read_text"

    def test_read_text_index_detected(self, tmp_path):
        hits = self._run(
            tmp_path,
            "from pathlib import Path\n" "def f(p):\n" "    text = p.read_text()\n" '    assert text.index("token") == 42\n',
        )
        assert len(hits) == 1

    def test_read_text_rfind_detected(self, tmp_path):
        hits = self._run(
            tmp_path,
            "from pathlib import Path\n" "def f(p):\n" "    text = p.read_text()\n" '    assert text.rfind("token") > 10\n',
        )
        assert len(hits) == 1

    def test_read_text_direct_inline_find_detected(self, tmp_path):
        hits = self._run(
            tmp_path,
            "from pathlib import Path\n" "def f(p):\n" '    assert p.read_text().find("token") > 10\n',
        )
        assert len(hits) == 1
        assert hits[0][1] == "read_text"

    def test_read_text_membership_not_flagged(self, tmp_path):
        # Deliberately out of scope for check 2 -- see module docstring
        # (test_storage_protocol.py's real, legitimate use of this shape).
        hits = self._run(
            tmp_path,
            "from pathlib import Path\n" "def f(p):\n" "    content = p.read_text()\n" '    assert "PROMPT TEXT" in content\n',
        )
        assert hits == []

    def test_read_text_regex_search_not_flagged(self, tmp_path):
        # Deliberately out of scope for check 2 -- see module docstring
        # (test_readme_quickstart.py / test_version_consistency.py's real,
        # legitimate extract-then-use-for-real of this shape).
        hits = self._run(
            tmp_path,
            "import re\n" "from pathlib import Path\n" "def f(p):\n" "    text = p.read_text()\n" '    m = re.search(r"version = \\"(.*)\\"", text)\n' "    assert m is not None\n",
        )
        assert hits == []

    # --- legitimate structural/documentation uses stay silent -----------

    def test_ast_get_docstring_not_flagged(self, tmp_path):
        # The repo's own pattern for a docstring-content check -- never
        # calls getsource/read_text at all.
        hits = self._run(
            tmp_path,
            "import ast\n" "def f(node):\n" '    doc = ast.get_docstring(node) or ""\n' '    assert "runner calls" in doc\n',
        )
        assert hits == []

    def test_ast_parse_structural_use_not_flagged(self, tmp_path):
        # Reading a file to walk its AST (this repo's own meta-linter
        # convention) is not a membership/find/regex proxy shape.
        hits = self._run(
            tmp_path,
            "import ast\n" "from pathlib import Path\n" "def f(p):\n" "    src = p.read_text()\n" "    tree = ast.parse(src)\n" "    return tree\n",
        )
        assert hits == []

    def test_getsource_loc_budget_not_flagged(self, tmp_path):
        # A pure line-count sensor on getsource() output is not a
        # membership/find/regex proxy shape either.
        hits = self._run(
            tmp_path,
            "import inspect\n" "def f(func):\n" "    src = inspect.getsource(func)\n" "    assert len(src.splitlines()) < 50\n",
        )
        assert hits == []

    def test_scoping_does_not_leak_across_functions(self, tmp_path):
        # A plain-string "text" in one function must not be conflated with
        # a read_text-bound "text" in an unrelated function.
        hits = self._run(
            tmp_path,
            "from pathlib import Path\n"
            "def f():\n"
            '    text = "just a literal string"\n'
            '    return text.find("x")\n'
            "def g(p):\n"
            "    text = p.read_text()\n"
            '    return text.find("y")\n',
        )
        assert len(hits) == 1
        assert hits[0][0] == 7  # only g()'s read_text-bound find, not f()'s

    def test_clean_file_silent(self, tmp_path):
        hits = self._run(
            tmp_path,
            "def add(a, b):\n" "    return a + b\n" "def test_add():\n" "    assert add(2, 3) == 5\n",
        )
        assert hits == []
