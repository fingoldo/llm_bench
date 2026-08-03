"""Static regression guard for the "unvalidated identifier flows into a
dangerous sink" vulnerability shape -- two of the audit's highest-severity
findings share this exact shape:

  (a) ``schema_name`` was f-string-interpolated into 13-20 SQL sites in
      ``storage/postgres.py``, each suppressed with ``# nosec B608``,
      justified only by a docstring claim ("operator-supplied") that
      nothing in ``__init__`` actually enforced at runtime -- independently
      rediscovered by 8 of 10 review agents. Now fixed: ``__init__``
      validates ``schema_name`` against ``_SCHEMA_NAME_RE.fullmatch(...)``
      and raises before assignment (see ``PostgresStorage.__init__``).
  (b) ``experiment_tag`` flowed unchecked into ``Path(root) / experiment_tag``
      across 5 ``FileStorage`` methods in ``storage/file.py``, with a
      proof-of-concept confirming both arbitrary file write and arbitrary
      recursive ``shutil.rmtree`` outside ``root`` -- a real path-traversal
      vulnerability. Now fixed via ``_tag_dir()``'s
      ``validate_experiment_tag()`` + ``.is_relative_to()`` guard.

Rather than re-pin those two ALREADY-fixed call sites by name (that's what
``test_storage_backend_hardening.py``'s ``scan_schema_name_validation`` /
``scan_experiment_tag_guard`` checks already do), this is a GENERIC
AST scanner over the whole ``src/llm_bench`` tree that catches the same
vulnerability CLASS wherever it next appears -- including in code that
doesn't exist yet:

  1. ``scan_nosec_b608_unvalidated_identifiers`` -- finds every
     ``# nosec B608`` comment, traces the f-string's interpolated
     ``self.<attr>`` name(s) back to the ``self.<attr> = <param>``
     assignment in the owning class's ``__init__``, and flags it if
     ``__init__`` never runs a validation call (``.fullmatch``/``.match``/
     ``.isidentifier()``/an explicit ``in``/``not in`` allowlist check)
     against that parameter, paired with a ``raise``, before the
     assignment.
  2. ``scan_unvalidated_path_joins`` -- finds every ``Path(...)/<name>``
     (including via a ``self.<attr>`` base that is itself Path-shaped, and
     chained ``a / b / c`` joins) or ``os.path.join(...)`` construction
     where the joined component traces back to a raw, self-attribute-
     assigned ``__init__`` parameter, and flags it under the same
     validation criterion as (1).

Both checks are pure AST pattern-matching, not real data-flow analysis:
they only trace a component that is EITHER a direct ``self.<attr>``
(assigned via a plain ``self.<attr> = <name>`` in ``__init__``) OR, for
check 2, a bare ``__init__``-local parameter name used directly inside
``__init__`` itself. A component that passes through an intermediate local
variable, a per-METHOD parameter (e.g. ``experiment_tag`` in
``FileStorage._tag_dir``, which is validated inside that method, not in
``__init__`` -- already covered by
``test_storage_backend_hardening.py::scan_experiment_tag_guard``), or a
transform (``self._schema = schema_name.lower()``) is out of scope and
silently skipped -- a deliberate precision-over-recall trade-off to keep
noise low. This IS real, moderate false-positive/false-negative risk (a
validated-but-differently-shaped guard, or a per-method rather than
per-``__init__`` validation, can both be misread) -- so, per this task's
instructions, findings are baseline-JSON-ratcheted (see
``test_code_audit_baseline.py`` for the same pattern applied to pyutilz's
generic checks) rather than a hard assert-empty: only a NEW finding not
already in ``_unvalidated_identifier_baseline.json`` fails the test.

Refresh after a deliberate change (new confirmed-safe pattern, or a real
fix that drains a finding):
    pytest tests/test_meta/test_unvalidated_identifier_flows.py --refresh-unvalidated-identifier-baseline
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench"
_BASELINE_PATH = Path(__file__).resolve().parent / "_unvalidated_identifier_baseline.json"

_REFRESH_FLAG = "--refresh-unvalidated-identifier-baseline"

_CHECK_NOSEC = "nosec_b608_unvalidated_identifier"
_CHECK_PATH_JOIN = "unvalidated_path_join"

# Calls that count as "validating" an identifier before it reaches a
# dangerous sink (SQL f-string / filesystem path).
_VALIDATION_CALL_NAMES = frozenset({"fullmatch", "match", "isidentifier"})


@dataclass(frozen=True)
class Finding:
    check: str
    file: str
    line: int
    message: str
    qualifier: str = ""

    @property
    def key(self) -> str:
        # `qualifier` disambiguates two distinct findings that happen to
        # land on the same file:line (e.g. two different self.<attr>
        # names interpolated on the same nosec-suppressed statement).
        base = f"{self.check}::{self.file}:{self.line}"
        return f"{base}::{self.qualifier}" if self.qualifier else base

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.message}"


# ------------------------------------------------------------------------
# Shared AST helpers
# ------------------------------------------------------------------------


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_class(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.ClassDef | None:
    cur: ast.AST | None = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.ClassDef):
            return cur
        cur = parents.get(cur)
    return None


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    cur: ast.AST | None = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    return None


def _find_init(cls: ast.ClassDef) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for item in cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
            return item
    return None


def _param_for_self_attr(init_fn: ast.FunctionDef | ast.AsyncFunctionDef, attr: str) -> tuple[str, int] | None:
    """If ``init_fn`` contains a plain ``self.<attr> = <name>`` (or
    annotated ``self.<attr>: T = <name>``) assignment, return
    ``(name, lineno)`` -- the raw parameter it was copied from, and where.
    Anything else (a transform, a literal, a call result) is untraceable
    and returns None."""
    for node in ast.walk(init_fn):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self" and target.attr == attr:
            if isinstance(value, ast.Name):
                return value.id, node.lineno
    return None


def _is_validating_expr(node: ast.AST, param_name: str) -> bool:
    """True if ``node`` is itself a fullmatch/match/isidentifier call, or an
    ``in``/``not in`` comparison, that references ``param_name`` -- i.e. a
    single expression that could plausibly gate a ``raise``."""
    if isinstance(node, ast.Call):
        func = node.func
        call_name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
        if call_name not in _VALIDATION_CALL_NAMES:
            return False
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        return param_name in names
    if isinstance(node, ast.Compare):
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            return False
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        return param_name in names
    return False


def _test_contains_validation(test_node: ast.expr, param_name: str) -> bool:
    """True if a validating call/compare referencing ``param_name`` appears
    anywhere inside an ``if``'s test expression (including combined via
    ``and``/``or``/``not``)."""
    return any(_is_validating_expr(n, param_name) for n in ast.walk(test_node))


def _validated_before(init_fn: ast.FunctionDef | ast.AsyncFunctionDef, param_name: str, before_lineno: int) -> bool:
    """True if ``init_fn`` contains an ``ast.If`` node, strictly before
    ``before_lineno``, whose ``test`` references a validation call
    (fullmatch/match/isidentifier) or an explicit ``in``/``not in``
    allowlist comparison against ``param_name``, AND whose ``body`` (the
    branch taken when that test is truthy) contains a ``raise`` -- i.e. the
    validation result must actually GATE the raise, not merely coexist
    somewhere in the function. This deliberately rejects the "call present
    but discarded" / "raise fires on an unrelated condition" bypass shape."""
    for node in ast.walk(init_fn):
        if not isinstance(node, ast.If):
            continue
        if node.lineno >= before_lineno:
            continue
        if not _test_contains_validation(node.test, param_name):
            continue
        if any(isinstance(n, ast.Raise) for n in ast.walk(ast.Module(body=node.body, type_ignores=[]))):
            return True
    return False


# ------------------------------------------------------------------------
# Check 1: "# nosec B608" comments -> unvalidated self.<attr> in an f-string
# ------------------------------------------------------------------------


def _self_attr_names_in_fstrings(node: ast.AST) -> set[str]:
    """Every ``self.<attr>`` name reachable INSIDE an f-string's
    interpolated ``{expr}`` part (``FormattedValue``) somewhere under
    ``node`` -- the literal-text segments of the f-string are irrelevant
    here since only interpolated values can carry attacker-controlled
    content into the rendered SQL."""
    attrs: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.JoinedStr):
            continue
        for part in sub.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            for n in ast.walk(part.value):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
                    attrs.add(n.attr)
    return attrs


def _smallest_enclosing_stmt(stmt_nodes: list[ast.stmt], lineno: int) -> ast.stmt | None:
    best: ast.stmt | None = None
    best_span: int | None = None
    for n in stmt_nodes:
        end = getattr(n, "end_lineno", None) or n.lineno
        if n.lineno <= lineno <= end:
            span = end - n.lineno
            if best_span is None or span < best_span:
                best, best_span = n, span
    return best


def _no_class_findings(rel: str, enclosing: ast.stmt, attrs: set[str]) -> list[Finding]:
    return [
        Finding(
            _CHECK_NOSEC, rel, enclosing.lineno,
            f"self.{attr} is f-string-interpolated into a `# nosec B608`-suppressed "
            f"string outside any class -- cannot verify a validated __init__ backs the "
            f"suppression. Wrap it in a class with an __init__ that validates and raises.",
            attr,
        )
        for attr in sorted(attrs)
    ]


def _no_init_findings(rel: str, enclosing: ast.stmt, cls: ast.ClassDef, attrs: set[str]) -> list[Finding]:
    return [
        Finding(
            _CHECK_NOSEC, rel, enclosing.lineno,
            f"{cls.name}.self.{attr} is f-string-interpolated into a `# nosec B608`-"
            f"suppressed string, but {cls.name} has no __init__ to validate it -- add "
            f"one, or validate {attr} before use.",
            attr,
        )
        for attr in sorted(attrs)
    ]


def _unvalidated_attr_findings(
    rel: str, enclosing: ast.stmt, cls: ast.ClassDef, init_fn: ast.FunctionDef | ast.AsyncFunctionDef, attrs: set[str]
) -> list[Finding]:
    out: list[Finding] = []
    for attr in sorted(attrs):
        traced = _param_for_self_attr(init_fn, attr)
        if traced is None:
            continue  # not a plain self.<attr> = <param> copy -- can't trace, skip
        param_name, assign_lineno = traced
        if _validated_before(init_fn, param_name, assign_lineno):
            continue
        out.append(
            Finding(
                _CHECK_NOSEC, rel, enclosing.lineno,
                f"{cls.name}.__init__ assigns self.{attr} from parameter {param_name!r} "
                f"(line {assign_lineno}) with no fullmatch/match/isidentifier/explicit-"
                f"allowlist check + raise validating {param_name!r} first, yet self.{attr} "
                f"is f-string-interpolated into a `# nosec B608`-suppressed SQL string "
                f"here -- SQL-injection-shaped gap. Add e.g. `if not "
                f"_SOME_RE.fullmatch({param_name}): raise ValueError(...)` before the "
                f"assignment, or refresh the baseline if this is a confirmed false "
                f"positive.",
                attr,
            )
        )
    return out


def _findings_for_nosec_stmt(rel: str, enclosing: ast.stmt, parents: dict[ast.AST, ast.AST]) -> list[Finding]:
    attrs = _self_attr_names_in_fstrings(enclosing)
    if not attrs:
        return []  # nosec suppression not actually f-string-interpolating a self.<attr>
    cls = _enclosing_class(enclosing, parents)
    if cls is None:
        return _no_class_findings(rel, enclosing, attrs)
    init_fn = _find_init(cls)
    if init_fn is None:
        return _no_init_findings(rel, enclosing, cls, attrs)
    return _unvalidated_attr_findings(rel, enclosing, cls, init_fn, attrs)


def scan_nosec_b608_unvalidated_identifiers(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(root))
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as e:
            findings.append(Finding(_CHECK_NOSEC, rel, e.lineno or 0, f"SyntaxError: {e}", "syntax"))
            continue
        lines = src.splitlines()
        nosec_lines = sorted(i + 1 for i, line in enumerate(lines) if "# nosec B608" in line)
        if not nosec_lines:
            continue
        parents = _parent_map(tree)
        stmt_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
        handled_stmt_ids: set[int] = set()
        for lineno in nosec_lines:
            enclosing = _smallest_enclosing_stmt(stmt_nodes, lineno)
            if enclosing is None:
                findings.append(
                    Finding(
                        _CHECK_NOSEC, rel, lineno,
                        "`# nosec B608` has no enclosing statement -- cannot trace the "
                        "interpolated identifier back to __init__. Move the suppression "
                        "onto a real statement or remove it.",
                        "no-stmt",
                    )
                )
                continue
            if id(enclosing) in handled_stmt_ids:
                continue  # one multi-line f-string can carry several `# nosec B608` comments
            handled_stmt_ids.add(id(enclosing))
            findings.extend(_findings_for_nosec_stmt(rel, enclosing, parents))
    return findings


# ------------------------------------------------------------------------
# Check 2: Path(...)/<name> or os.path.join(...) -> unvalidated component
# ------------------------------------------------------------------------


def _is_path_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "Path":
        return True
    return bool(isinstance(func, ast.Attribute) and func.attr == "Path")


def _is_path_shaped_base(node: ast.AST) -> bool:
    """True for the LEFT side of a ``/`` join that plausibly builds a
    filesystem path: a direct ``Path(...)``/``pathlib.Path(...)`` call, a
    ``self.<attr>`` access (the common case -- ``self.root = Path(root)``
    assigned once in __init__, then joined elsewhere), or another such
    join (``a / b / c`` chains, e.g. ``self.root / "sub" / tag``)."""
    if _is_path_call(node):
        return True
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return True
    return bool(isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div))


def _is_os_path_join_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "join"):
        return False
    owner = func.value
    return isinstance(owner, ast.Attribute) and owner.attr == "path" and isinstance(owner.value, ast.Name) and owner.value.id == "os"


def _trace_component_to_init_param(
    component: ast.expr, construct: ast.AST, parents: dict[ast.AST, ast.AST]
) -> tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, str, int] | None:
    """Trace a joined path component back to a raw, unvalidated
    ``__init__`` parameter. Two shapes are handled:

      1. ``self.<attr>`` -- traced via ``_param_for_self_attr`` to the
         ``self.<attr> = <param>`` assignment in the owning class's
         ``__init__`` (the realistic shape: the attribute is set once at
         construction time and joined onto a path in a later method).
      2. A bare ``Name`` used INSIDE ``__init__`` itself, where that name
         is one of ``__init__``'s own parameters (the join happens before
         the parameter is ever copied to an attribute).

    Anything else -- a local variable, a per-method parameter, a computed
    expression -- can't be traced without real data-flow analysis and is
    deliberately left out of scope (returns None)."""
    if isinstance(component, ast.Attribute) and isinstance(component.value, ast.Name) and component.value.id == "self":
        cls = _enclosing_class(construct, parents)
        if cls is None:
            return None
        init_fn = _find_init(cls)
        if init_fn is None:
            return None
        traced = _param_for_self_attr(init_fn, component.attr)
        if traced is None:
            return None
        param_name, assign_lineno = traced
        return cls.name, init_fn, param_name, assign_lineno
    if isinstance(component, ast.Name):
        enclosing_fn = _enclosing_function(construct, parents)
        if enclosing_fn is None or enclosing_fn.name != "__init__":
            return None
        params = {a.arg for a in enclosing_fn.args.args} | {a.arg for a in enclosing_fn.args.kwonlyargs}
        if component.id not in params:
            return None
        cls = _enclosing_class(construct, parents)
        if cls is None:
            return None
        construct_lineno = getattr(construct, "lineno", enclosing_fn.lineno)
        return cls.name, enclosing_fn, component.id, construct_lineno
    return None


def scan_unvalidated_path_joins(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(root))
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as e:
            findings.append(Finding(_CHECK_PATH_JOIN, rel, e.lineno or 0, f"SyntaxError: {e}", "syntax"))
            continue
        parents = _parent_map(tree)
        seen: set[tuple[int, str]] = set()
        for node in ast.walk(tree):
            pairs: list[tuple[ast.expr, ast.expr]] = []
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and _is_path_shaped_base(node.left):
                pairs.append((node, node.right))
            elif _is_os_path_join_call(node):
                assert isinstance(node, ast.Call)
                pairs.extend((node, arg) for arg in node.args[1:])
            for construct, component in pairs:
                if isinstance(component, ast.Constant):
                    continue  # hardcoded path segment -- not attacker-controlled
                traced = _trace_component_to_init_param(component, construct, parents)
                if traced is None:
                    continue
                cls_name, init_fn, param_name, before_lineno = traced
                dedup_key = (construct.lineno, param_name)
                if dedup_key in seen:
                    continue
                if _validated_before(init_fn, param_name, before_lineno):
                    continue
                seen.add(dedup_key)
                findings.append(
                    Finding(
                        _CHECK_PATH_JOIN, rel, construct.lineno,
                        f"{cls_name}: filesystem-path construction here uses a component "
                        f"traced to __init__ parameter {param_name!r}, but __init__ never "
                        f"validates it (fullmatch/match/isidentifier/explicit-allowlist + "
                        f"raise) before it flows into Path(...)/os.path.join(...) -- "
                        f"path-traversal-shaped gap. Add a validation call + raise in "
                        f"__init__ before {param_name!r} is used, or refresh the baseline "
                        f"if this is a confirmed false positive.",
                        param_name,
                    )
                )
    return findings


# ------------------------------------------------------------------------
# Baseline-ratcheted test (moderate FP risk -- see module docstring)
# ------------------------------------------------------------------------


def _refresh_requested() -> bool:
    return _REFRESH_FLAG in sys.argv


def _all_findings(root: Path) -> list[Finding]:
    return scan_nosec_b608_unvalidated_identifiers(root) + scan_unvalidated_path_joins(root)


def test_no_new_unvalidated_identifier_findings():
    current = _all_findings(_SRC_ROOT)
    current_by_key = {f.key: f for f in current}
    current_keys = set(current_by_key)

    if _refresh_requested() or not _BASELINE_PATH.exists():
        _BASELINE_PATH.write_text(json.dumps(sorted(current_keys), indent=2), encoding="utf-8")
        n = len(current_keys)
        pytest.skip(f"unvalidated-identifier baseline refreshed at {_BASELINE_PATH.name} ({n} existing finding(s))")

    baseline = set(json.loads(_BASELINE_PATH.read_text(encoding="utf-8")))
    new = sorted(current_keys - baseline)
    if new:
        detail_lines = [str(current_by_key[k]) for k in new]
        msg = "\n  ".join(detail_lines)
        pytest.fail(
            f"{len(new)} new unvalidated-identifier finding(s) (SQL f-string / filesystem-"
            f"path injection shape -- see module docstring). Fix the code (add a "
            f"fullmatch/isidentifier check + raise in __init__ before the parameter is "
            f"used), OR if this is a confirmed false positive, refresh the baseline after "
            f"review: pytest tests/test_meta/test_unvalidated_identifier_flows.py "
            f"{_REFRESH_FLAG}\n  {msg}"
        )


# ------------------------------------------------------------------------
# Regression tests for the checkers themselves (synthetic source, tmp_path)
# ------------------------------------------------------------------------


class TestNosecB608ScannerDetectsShapes:
    def _run(self, tmp_path, source: str) -> list[Finding]:
        p = tmp_path / "synthetic_postgres.py"
        p.write_text(source, encoding="utf-8")
        return scan_nosec_b608_unvalidated_identifiers(tmp_path)

    def test_unvalidated_schema_name_fires(self, tmp_path):
        source = (
            "class Storage:\n"
            "    def __init__(self, schema_name='llm_bench'):\n"
            "        self._schema = schema_name\n"
            "\n"
            "    async def get(self, conn):\n"
            "        return await conn.fetchrow(\n"
            "            f'SELECT * FROM {self._schema}.t WHERE x=1'  # nosec B608\n"
            "        )\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "schema_name" in violations[0].message

    def test_validated_schema_name_is_clean(self, tmp_path):
        source = (
            "import re\n"
            "_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')\n"
            "class Storage:\n"
            "    def __init__(self, schema_name='llm_bench'):\n"
            "        if not _RE.fullmatch(schema_name):\n"
            "            raise ValueError('bad schema_name')\n"
            "        self._schema = schema_name\n"
            "\n"
            "    async def get(self, conn):\n"
            "        return await conn.fetchrow(\n"
            "            f'SELECT * FROM {self._schema}.t WHERE x=1'  # nosec B608\n"
            "        )\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []

    def test_discarded_check_and_unrelated_raise_still_fires(self, tmp_path):
        # Adversarial shape: a validation call IS present, but its result is
        # thrown away, and the `raise` is gated on an unrelated condition --
        # schema_name is genuinely never validated. Must still fire.
        source = (
            "class Storage:\n"
            "    def __init__(self, schema_name, flag):\n"
            "        _ = schema_name.isidentifier()\n"
            "        if flag:\n"
            "            raise RuntimeError('unrelated to schema_name')\n"
            "        self._schema = schema_name\n"
            "\n"
            "    async def get(self, conn):\n"
            "        return await conn.fetchrow(\n"
            "            f'SELECT * FROM {self._schema}.t WHERE x=1'  # nosec B608\n"
            "        )\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "schema_name" in violations[0].message

    def test_non_self_interpolation_is_ignored(self, tmp_path):
        # The nosec-suppressed f-string interpolates a module-level constant,
        # not a self.<attr> -- nothing to trace back to __init__, so this
        # checker (which is scoped to the self.<attr> shape) stays silent.
        source = (
            "_COLS = 'a, b, c'\n"
            "class Storage:\n"
            "    def __init__(self, schema_name='llm_bench'):\n"
            "        self._schema = schema_name\n"
            "\n"
            "    async def get(self, conn):\n"
            "        return await conn.fetchrow(\n"
            "            f'SELECT {_COLS} FROM t'  # nosec B608\n"
            "        )\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []


class TestUnvalidatedPathJoinScannerDetectsShapes:
    def _run(self, tmp_path, source: str) -> list[Finding]:
        p = tmp_path / "synthetic_file.py"
        p.write_text(source, encoding="utf-8")
        return scan_unvalidated_path_joins(tmp_path)

    def test_unvalidated_self_attr_join_fires(self, tmp_path):
        source = (
            "from pathlib import Path\n"
            "class Store:\n"
            "    def __init__(self, root, subdir):\n"
            "        self.root = Path(root)\n"
            "        self.subdir = subdir\n"
            "\n"
            "    def get_path(self):\n"
            "        return self.root / self.subdir\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "subdir" in violations[0].message

    def test_validated_self_attr_join_is_clean(self, tmp_path):
        source = (
            "from pathlib import Path\n"
            "class Store:\n"
            "    def __init__(self, root, subdir):\n"
            "        if not subdir.isidentifier():\n"
            "            raise ValueError('bad subdir')\n"
            "        self.root = Path(root)\n"
            "        self.subdir = subdir\n"
            "\n"
            "    def get_path(self):\n"
            "        return self.root / self.subdir\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []

    def test_constant_path_segment_is_ignored(self, tmp_path):
        source = (
            "from pathlib import Path\n"
            "class Store:\n"
            "    def __init__(self, root):\n"
            "        self.root = Path(root)\n"
            "\n"
            "    def get_path(self):\n"
            "        return self.root / '_index'\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []

    def test_unvalidated_os_path_join_fires(self, tmp_path):
        source = (
            "import os\n"
            "class Store:\n"
            "    def __init__(self, root, subdir):\n"
            "        self.root = root\n"
            "        self.subdir = subdir\n"
            "\n"
            "    def get_path(self):\n"
            "        return os.path.join(self.root, self.subdir)\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "subdir" in violations[0].message

    def test_discarded_check_and_unrelated_raise_still_fires(self, tmp_path):
        # Same adversarial shape as the nosec-B608 sibling test: a
        # validation call is present but its result is discarded, and the
        # `raise` is gated on an unrelated condition -- `subdir` is never
        # actually validated. Must still fire.
        source = (
            "from pathlib import Path\n"
            "class Store:\n"
            "    def __init__(self, root, subdir, flag):\n"
            "        _ = subdir.isidentifier()\n"
            "        if flag:\n"
            "            raise ValueError('unrelated to subdir')\n"
            "        self.root = Path(root)\n"
            "        self.subdir = subdir\n"
            "\n"
            "    def get_path(self):\n"
            "        return self.root / self.subdir\n"
        )
        violations = self._run(tmp_path, source)
        assert len(violations) == 1
        assert "subdir" in violations[0].message

    def test_method_parameter_join_is_out_of_scope(self, tmp_path):
        # `tag` is a per-METHOD parameter, not traceable to __init__ --
        # this exact shape (a per-call parameter validated inside the
        # method, not __init__) is already covered by
        # test_storage_backend_hardening.py::scan_experiment_tag_guard;
        # this generic checker deliberately stays silent on it (real
        # data-flow tracing of a method-local parameter is out of scope,
        # see module docstring).
        source = (
            "from pathlib import Path\n"
            "class Store:\n"
            "    def __init__(self, root):\n"
            "        self.root = Path(root)\n"
            "\n"
            "    def get_path(self, tag):\n"
            "        return self.root / tag\n"
        )
        violations = self._run(tmp_path, source)
        assert violations == []
