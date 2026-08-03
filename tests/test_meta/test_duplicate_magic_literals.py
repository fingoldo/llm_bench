"""Catch a magic literal re-typed at 3+ call/definition sites across 2+
files with no shared named constant behind it.

Motivating audit finding: the "row usable" predicate's threshold
(``min_response_len: int = 20`` / a bare literal ``20``) was independently
re-typed across ``storage/{memory,file,postgres}.py`` and
``ranking/ranker.py`` before being consolidated into
``core.predicates.MIN_USABLE_RESPONSE_LEN``; separately, the string
``"openrouter"`` was hardcoded at 4 call sites in ``runner/round_runner.py``
instead of reading ``provider_factory``'s actual return value (now a single
``provider_label`` constant). This is a regression guard against the
pattern reappearing elsewhere: same keyword-argument name (or same
parameter's / dataclass field's default) carrying the same literal value,
written out by hand at 3+ sites in 2+ different files.

Only "interesting" literals count (see ``_is_interesting``): string
constants of length >= 4 that aren't a common stdlib/config token
(``"utf-8"``, HTTP verbs, file modes, ...), or int/float constants outside
``{-1, 0, 1, 2}``. A value referenced via a variable/import (``ast.Name``
rather than ``ast.Constant``) never counts -- that IS the "shared named
constant" case this check wants to see, so by construction only bare,
independently-retyped literals are flagged.

This still carries real false-positive risk (a value can legitimately
recur without warranting its own constant, e.g. two unrelated functions
both happening to default a `timeout` to the same number), so this uses
the baseline-JSON-ratchet pattern (see ``test_code_audit_baseline.py`` /
``test_api_stability.py``) rather than a hard assert-empty: only a NEW
duplicate-literal group fails the test. Refresh after a deliberate change:

    pytest tests/test_meta/test_duplicate_magic_literals.py --refresh-duplicate-magic-literals-baseline
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench"
_BASELINE_PATH = Path(__file__).resolve().parent / "_duplicate_magic_literals_baseline.json"

_REFRESH_FLAG = "--refresh-duplicate-magic-literals-baseline"

# A group must have at least this many occurrences...
_MIN_SITES = 3
# ...spanning at least this many distinct files, to count as a finding.
_MIN_FILES = 2

_MIN_STRING_LEN = 4
_COMMON_INT_FLOAT_VALUES = frozenset({-1, 0, 1, 2})

# Strings excluded even though len >= 4 -- these are common stdlib/config
# tokens (encodings, HTTP methods, file modes, log levels, ...) that
# legitimately recur without warranting a shared constant; flagging them
# would just be noise, not the "someone forgot to define a constant" smell
# this check targets.
_COMMON_STRING_TOKENS: frozenset[str] = frozenset(
    {
        "utf-8",
        "utf8",
        "utf-8-sig",
        "ascii",
        "latin-1",
        "latin1",
        "ignore",
        "strict",
        "replace",
        "get",
        "post",
        "put",
        "patch",
        "head",
        "delete",
        "options",
        "http",
        "https",
        "true",
        "false",
        "none",
        "null",
    }
)

# (kwarg-or-param-name, repr(value)) pairs allowed to repeat -- confirmed
# false positives with a one-line rationale. Empty: no confirmed FPs yet.
_WHITELIST: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class _Occurrence:
    name: str
    value_repr: str
    file: str
    line: int


def _is_interesting(value: object) -> bool:
    """True when ``value`` is worth tracking as a candidate magic literal.

    Booleans are a subtype of ``int`` in Python but are never "magic" here
    -- excluded explicitly, though in practice ``True``/``False`` would
    also fall out via the ``{-1, 0, 1, 2}`` exclusion since ``True == 1``
    and ``False == 0``.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        if len(value) < _MIN_STRING_LEN:
            return False
        return value.lower() not in _COMMON_STRING_TOKENS
    if isinstance(value, (int, float)):
        return value not in _COMMON_INT_FLOAT_VALUES
    return False


def _defaults_occurrences(args: ast.arguments, rel: str) -> list[_Occurrence]:
    """(param-name, default-value) pairs from one function's signature.

    ``ast.arguments.defaults`` covers positional-or-keyword params and
    right-aligns to the tail of ``posonlyargs + args`` (fewer defaults
    than params means the first N params are required) -- see the
    ``ast`` grammar docs for ``arguments``. ``kw_defaults`` is already
    one-to-one with ``kwonlyargs``, with ``None`` marking a required
    keyword-only param.
    """
    out: list[_Occurrence] = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults = list(args.defaults)
    offset = len(positional) - len(defaults)
    for arg, default in zip(positional[offset:], defaults, strict=True):
        if isinstance(default, ast.Constant) and _is_interesting(default.value):
            out.append(_Occurrence(arg.arg, repr(default.value), rel, default.lineno))
    for kwarg, kw_default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if kw_default is not None and isinstance(kw_default, ast.Constant) and _is_interesting(kw_default.value):
            out.append(_Occurrence(kwarg.arg, repr(kw_default.value), rel, kw_default.lineno))
    return out


def _module_or_class_assign_occurrences(tree: ast.Module, rel: str) -> list[_Occurrence]:
    """(field-name, value) pairs from module- or class-level assignments
    with a plain ``Name`` target and a bare ``Constant`` value -- this is
    what a ``@dataclass`` field default (``provider_label: str = "openrouter"``)
    or a plain module-level config constant looks like in the AST.

    Deliberately does NOT descend into function/lambda bodies: a local
    variable assignment (``x = 20`` inside a function) is not the "config
    default duplicated across files" smell this check targets, so those
    scopes are walked (for nested classes/defs) but never sampled from.
    """
    out: list[_Occurrence] = []

    def walk(node: ast.AST, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                walk(child, True)
                continue
            if not in_function and isinstance(child, ast.AnnAssign):
                if isinstance(child.target, ast.Name) and isinstance(child.value, ast.Constant) and _is_interesting(child.value.value):
                    out.append(_Occurrence(child.target.id, repr(child.value.value), rel, child.lineno))
            elif not in_function and isinstance(child, ast.Assign):
                if isinstance(child.value, ast.Constant) and _is_interesting(child.value.value):
                    out.extend(_Occurrence(target.id, repr(child.value.value), rel, child.lineno) for target in child.targets if isinstance(target, ast.Name))
            walk(child, in_function)

    walk(tree, False)
    return out


def _collect_occurrences(path: Path, rel: str) -> list[_Occurrence]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []  # a syntax-error scan is another check's job, not this one's

    out: list[_Occurrence] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg is None:
                    continue  # **kwargs spread -- no literal name to key on
                if isinstance(kw.value, ast.Constant) and _is_interesting(kw.value.value):
                    out.append(_Occurrence(kw.arg, repr(kw.value.value), rel, kw.value.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            out.extend(_defaults_occurrences(node.args, rel))
    out.extend(_module_or_class_assign_occurrences(tree, rel))
    return out


def _scan(root: Path) -> dict[str, str]:
    """Scan every ``.py`` file under ``root`` and return
    ``{finding_key: message}`` for each (name, value) pair that is
    independently re-typed as a bare literal at >= ``_MIN_SITES`` sites
    spanning >= ``_MIN_FILES`` distinct files."""
    occurrences: list[_Occurrence] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(root))
        occurrences.extend(_collect_occurrences(path, rel))

    groups: dict[tuple[str, str], list[_Occurrence]] = {}
    for occ in occurrences:
        groups.setdefault((occ.name, occ.value_repr), []).append(occ)

    findings: dict[str, str] = {}
    for (name, value_repr), occs in sorted(groups.items()):
        if (name, value_repr) in _WHITELIST:
            continue
        files = sorted({o.file for o in occs})
        if len(occs) < _MIN_SITES or len(files) < _MIN_FILES:
            continue
        key = f"{name}={value_repr}"
        sites = ", ".join(f"{o.file}:{o.line}" for o in sorted(occs, key=lambda o: (o.file, o.line)))
        findings[key] = (
            f"{name}={value_repr} written as a bare literal at {len(occs)} site(s) across "
            f"{len(files)} file(s) with no shared named constant: {sites}. Extract one named "
            f"constant and import/reference it at every site, or whitelist in "
            f"_WHITELIST if this recurrence is a confirmed coincidence, not debt."
        )
    return findings


def test_no_new_duplicate_magic_literal_groups():
    current = _scan(_SRC_ROOT)
    current_keys = set(current)

    if _REFRESH_FLAG in sys.argv or not _BASELINE_PATH.exists():
        _BASELINE_PATH.write_text(
            json.dumps(sorted(current_keys), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        pytest.skip(f"baseline refreshed at {_BASELINE_PATH.name} " f"({len(current_keys)} existing group(s))")

    baseline = set(json.loads(_BASELINE_PATH.read_text(encoding="utf-8")))
    new = sorted(current_keys - baseline)
    if new:
        detail = "\n  ".join(current[k] for k in new)
        pytest.fail(
            f"{len(new)} new duplicate-magic-literal group(s):\n  {detail}\n"
            f"Fix by extracting a shared named constant, OR if a confirmed false "
            f"positive, add a one-line rationale to _WHITELIST, OR refresh the "
            f"baseline after review: pytest ... {_REFRESH_FLAG}"
        )


class TestScanDetectsShapes:
    """Direct regression tests for the checker itself, via synthetic
    source spread across real tmp_path files (multi-file duplication is
    the whole point of this check, so a single-string ``_collect_occurrences``
    call can't exercise it -- each test writes N sibling .py files and
    scans the directory, exactly like the real test does against src/)."""

    def _write(self, tmp_path, name: str, source: str) -> None:
        (tmp_path / name).write_text(source, encoding="utf-8")

    def test_fires_on_kwarg_literal_across_three_files(self, tmp_path):
        for n in ("a", "b", "c"):
            self._write(tmp_path, f"{n}.py", f"def f_{n}():\n    call(min_response_len=20)\n")
        findings = _scan(tmp_path)
        assert findings == {
            "min_response_len=20": (
                "min_response_len=20 written as a bare literal at 3 site(s) across 3 file(s) "
                "with no shared named constant: a.py:2, b.py:2, c.py:2. Extract one named "
                "constant and import/reference it at every site, or whitelist in "
                "_WHITELIST if this recurrence is a confirmed coincidence, not debt."
            )
        }

    def test_fires_on_dataclass_field_default_across_files(self, tmp_path):
        for n in ("a", "b", "c"):
            self._write(
                tmp_path,
                f"{n}.py",
                "from dataclasses import dataclass\n\n\n" "@dataclass\n" f"class Cfg{n}:\n" '    provider_label: str = "openrouter"\n',
            )
        findings = _scan(tmp_path)
        assert list(findings) == ["provider_label='openrouter'"]

    def test_silent_on_local_variable_assignment(self, tmp_path):
        # A plain local assignment inside a function body is not a config
        # default -- must never be sampled, however often it recurs.
        for n in ("a", "b", "c"):
            self._write(tmp_path, f"{n}.py", f'def f_{n}():\n    provider_label = "openrouter"\n    return provider_label\n')
        assert _scan(tmp_path) == {}

    def test_fires_on_default_param_literal_across_files(self, tmp_path):
        for n in ("a", "b", "c"):
            self._write(tmp_path, f"{n}.py", "def f(min_response_len: int = 20):\n    pass\n")
        findings = _scan(tmp_path)
        assert list(findings) == ["min_response_len=20"]

    def test_fires_on_string_literal_across_files(self, tmp_path):
        for n in ("a", "b", "c", "d"):
            self._write(tmp_path, f"{n}.py", f'def f_{n}():\n    get_llm_provider(provider_label="openrouter")\n')
        findings = _scan(tmp_path)
        assert list(findings) == ["provider_label='openrouter'"]

    def test_silent_when_only_two_sites(self, tmp_path):
        for n in ("a", "b"):
            self._write(tmp_path, f"{n}.py", "def f():\n    call(min_response_len=20)\n")
        assert _scan(tmp_path) == {}

    def test_silent_when_all_sites_share_one_file(self, tmp_path):
        source = "def f():\n    call(min_response_len=20)\n    call(min_response_len=20)\n    call(min_response_len=20)\n"
        self._write(tmp_path, "a.py", source)
        assert _scan(tmp_path) == {}

    def test_silent_when_value_referenced_via_named_constant(self, tmp_path):
        # Same effective value at 3 sites, but supplied through a Name
        # (an imported constant), never re-typed as a literal -- exactly
        # the pattern this check should NOT flag.
        self._write(tmp_path, "const.py", "MIN_LEN = 20\n")
        for n in ("a", "b", "c"):
            self._write(
                tmp_path,
                f"{n}.py",
                "from const import MIN_LEN\n" f"def f_{n}():\n    call(min_response_len=MIN_LEN)\n",
            )
        assert _scan(tmp_path) == {}

    def test_silent_on_common_string_token(self, tmp_path):
        for n in ("a", "b", "c"):
            self._write(tmp_path, f"{n}.py", f'def f_{n}():\n    open("x_{n}.txt", encoding="utf-8")\n')
        assert _scan(tmp_path) == {}

    def test_silent_on_short_string(self, tmp_path):
        for n in ("a", "b", "c"):
            self._write(tmp_path, f"{n}.py", f'def f_{n}():\n    call(mode="abc")\n')
        assert _scan(tmp_path) == {}

    def test_silent_on_common_int(self, tmp_path):
        for n in ("a", "b", "c"):
            self._write(tmp_path, f"{n}.py", f"def f_{n}():\n    call(retries=1)\n")
        assert _scan(tmp_path) == {}

    def test_whitelist_suppresses_confirmed_false_positive(self, tmp_path, monkeypatch):
        for n in ("a", "b", "c"):
            self._write(tmp_path, f"{n}.py", f"def f_{n}():\n    call(magic_flag=42)\n")
        monkeypatch.setattr(sys.modules[__name__], "_WHITELIST", {("magic_flag", "42")})
        assert _scan(tmp_path) == {}
