"""Catch Protocol call sites that don't honour a documented async-optional
return contract under src/llm_bench/.

Several framework extension points are formal ``@runtime_checkable
Protocol`` classes whose contract explicitly says the implementation MAY
return an ``Awaitable`` instead of a plain value (``TaskPool.sample()`` /
``total_size()`` in ``pool/base.py``; ``RowScorer.__call__`` in
``ranking/ranker.py``). ``Benchmark._sample_units()`` used to call
``self.task_pool.sample(n)`` synchronously and use the result directly,
a guaranteed ``TypeError: object of type 'coroutine' has no len()`` for
anyone who wrote an async ``TaskPool`` exactly matching the documented
contract (audit: 04-High). The fix (see ``runner/benchmark.py``,
``_sample_units()``) is the reference dispatch pattern this checker looks
for at every OTHER call site of a method the same kind of docstring
promises to be async-optional::

    sample_result = self.task_pool.sample(n)
    units = await sample_result if asyncio.iscoroutine(sample_result) \\
        else cast("list[TaskUnit]", sample_result)

A call site is considered safe when it either (a) sits directly inside an
``await`` expression, or (b) the result is assigned to a local variable
and the enclosing function checks ``asyncio.iscoroutine(...)`` or
``inspect.isawaitable(...)`` on that variable before using it.

This is the lowest-confidence check in the meta-test suite: it identifies
"async-optional" methods by regex-matching a Protocol's class/method
docstring for phrases like "MAY be async" or "async-aware", then flags
ANY call site anywhere in the tree whose attribute name matches,
without resolving whether the receiver is actually that Protocol. A
generically-named method (``sample``, ``total_size``, ...) reused by an
unrelated class for an unrelated (always-synchronous) purpose would be a
false positive. Shipped as an advisory baseline-JSON-ratchet check for
that reason: only a NEW finding fails the test. Refresh with
``--refresh-protocol-async-dispatch-baseline`` after reviewing a
newly-surfaced finding (fix the call site, or if it's a confirmed false
positive, refresh so it's silently tracked going forward).

Known heuristic gaps beyond the false-positive risk above (documented
here, not fixed, per the "advisory/lowest-confidence" framing):

* ``_scope_checks_awaitable`` only requires the *textual presence* of an
  ``iscoroutine``/``isawaitable`` call on the same variable name
  somewhere in the enclosing function -- it does not verify that check
  actually gates the subsequent use. ``result = w.fetch(3); if
  asyncio.iscoroutine(result): log(...); return result`` (never awaited)
  is silently treated as safe.
* The scan does not follow calls into helper functions: wrapping the
  dispatch idiom in a shared ``async def _maybe_await(x): return await x
  if asyncio.iscoroutine(x) else x`` helper and calling
  ``await _maybe_await(result)`` is a false positive, because only the
  immediately-enclosing function's body is walked for the guard.
* The scanner keys exclusively on dotted ``<obj>.<method>(...)`` call
  syntax with the Protocol's *method name* as the attribute. Extension
  points invoked by a different attribute name (e.g. a ``PromptBuilder``
  stored as ``stage.prompt_builder`` and called as
  ``stage.prompt_builder(...)``, or a ``RowScorer`` called bare as
  ``scorer(row)``) are invisible to this checker regardless of whether
  ``_ASYNC_OPTIONAL_RE`` matches their docstring -- a regex fix alone
  would not surface those call sites.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench"
_BASELINE_PATH = Path(__file__).resolve().parent / "_protocol_async_optional_dispatch_baseline.json"

_REFRESH_FLAG = "--refresh-protocol-async-dispatch-baseline"

# Phrases that mark a Protocol class/method docstring as documenting an
# async-optional return contract ("MAY return X | Awaitable[X], the
# caller awaits it"). Deliberately fuzzy, see module docstring.
_ASYNC_OPTIONAL_RE = re.compile(
    r"MAY be async|async-aware|coroutine.*works too|awaits any awaitable",
    re.IGNORECASE | re.DOTALL,
)

_PROTOCOL_BASE_NAMES = frozenset({"Protocol", "typing.Protocol"})
_RUNTIME_CHECKABLE_NAMES = frozenset({"runtime_checkable", "typing.runtime_checkable"})

# Recognised "did you check before using it" idioms.
_AWAIT_CHECK_FUNCS = frozenset({"iscoroutine", "isawaitable"})
_AWAIT_CHECK_MODULES = frozenset({"asyncio", "inspect"})

# Per-file whitelist for confirmed false positives: "<rel_path>::<line>".
_WHITELIST: set[str] = set()


def _dotted_name(node: ast.expr | None) -> str:
    """Best-effort ``a.b.c`` rendering of a Name/Attribute/Subscript/Call
    expression, used to recognise ``Protocol`` / ``runtime_checkable``
    regardless of whether they were imported bare or via ``typing.``."""
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Subscript):
        return _dotted_name(node.value)  # e.g. Protocol[T] -> "Protocol"
    if isinstance(node, ast.Call):
        return _dotted_name(node.func)  # e.g. a bare-call decorator form
    return ""


def _is_protocol_class(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if _dotted_name(base) in _PROTOCOL_BASE_NAMES:
            return True
    for dec in node.decorator_list:
        if _dotted_name(dec) in _RUNTIME_CHECKABLE_NAMES:
            return True
    return False


def _collect_async_optional_methods(root: Path) -> set[str]:
    """Step 1: every Protocol class's/method's docstring matching
    ``_ASYNC_OPTIONAL_RE`` contributes its method name(s) to the returned
    set. A class-level match applies to every method on that class; a
    method-level match applies to just that method."""
    method_names: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _is_protocol_class(node):
                continue
            class_matches = bool(_ASYNC_OPTIONAL_RE.search(ast.get_docstring(node) or ""))
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                method_matches = class_matches or bool(_ASYNC_OPTIONAL_RE.search(ast.get_docstring(item) or ""))
                if method_matches:
                    method_names.add(item.name)
    return method_names


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    return None


def _is_directly_awaited(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(call)
    return isinstance(parent, ast.Await) and parent.value is call


def _assigned_var_names(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> list[str]:
    """If ``call`` is the RHS value of a simple/annotated/walrus assignment,
    return the Name id(s) it's bound to; these are the variables an
    ``iscoroutine``/``isawaitable`` guard would plausibly check."""
    parent = parents.get(call)
    names: list[str] = []
    if isinstance(parent, ast.Assign) and parent.value is call:
        names.extend(t.id for t in parent.targets if isinstance(t, ast.Name))
    elif isinstance(parent, ast.AnnAssign) and parent.value is call and isinstance(parent.target, ast.Name):
        names.append(parent.target.id)
    elif isinstance(parent, ast.NamedExpr) and parent.value is call and isinstance(parent.target, ast.Name):
        names.append(parent.target.id)
    return names


def _scope_checks_awaitable(scope: ast.AST, var_names: set[str]) -> bool:
    """Ancestor-scan ``scope`` (the enclosing function, or the whole
    module for a call outside any function) for ``asyncio.iscoroutine(v)``
    / ``inspect.isawaitable(v)`` where ``v`` is one of ``var_names``."""
    if not var_names:
        return False
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _AWAIT_CHECK_FUNCS:
            continue
        mod = node.func.value
        if not (isinstance(mod, ast.Name) and mod.id in _AWAIT_CHECK_MODULES):
            continue
        if any(isinstance(arg, ast.Name) and arg.id in var_names for arg in node.args):
            return True
    return False


def scan_for_dispatch_violations(root: Path) -> list[str]:
    """Step 2: every ``<something>.<method>(...)`` call site anywhere
    under ``root`` where ``method`` is async-optional (per
    ``_collect_async_optional_methods``) is flagged unless it's directly
    ``await``-ed or its result is checked with
    ``asyncio.iscoroutine()``/``inspect.isawaitable()`` before use."""
    method_names = _collect_async_optional_methods(root)
    violations: list[str] = []
    if not method_names:
        return violations

    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as e:
            violations.append(f"{rel}:0: SyntaxError: {e}")
            continue

        parents = _build_parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in method_names:
                continue
            whitelist_key = f"{rel}::{node.lineno}"
            if whitelist_key in _WHITELIST:
                continue
            if _is_directly_awaited(node, parents):
                continue
            var_names = set(_assigned_var_names(node, parents))
            scope = _enclosing_function(node, parents) or tree
            if _scope_checks_awaitable(scope, var_names):
                continue
            violations.append(
                f"{rel}:{node.lineno}: `.{node.func.attr}(...)` call targets a Protocol method "
                f"documented as async-optional, but this call site neither `await`s it directly "
                f"nor checks asyncio.iscoroutine()/inspect.isawaitable() on the result before use "
                f"(see Benchmark._sample_units() in runner/benchmark.py for the reference pattern)."
            )
    return violations


def _refresh_requested() -> bool:
    return _REFRESH_FLAG in sys.argv


def _baseline_key(violation: str) -> str:
    """``"<rel>:<lineno>: <message>"`` -> ``"<rel>:<lineno>"`` - the
    baseline is keyed by call-site location, not by the message text, so
    wording tweaks to the check don't spuriously "un-ratchet" it."""
    return violation.split(": ", 1)[0]


def test_protocol_async_optional_dispatch_matches_baseline():
    current = sorted(scan_for_dispatch_violations(_SRC_ROOT))
    current_keys = {_baseline_key(v): v for v in current}

    if _refresh_requested() or not _BASELINE_PATH.exists():
        _BASELINE_PATH.write_text(
            json.dumps(sorted(current_keys), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        pytest.skip(f"baseline refreshed at {_BASELINE_PATH.name} ({len(current_keys)} existing finding(s))")

    baseline = set(json.loads(_BASELINE_PATH.read_text(encoding="utf-8")))
    new = sorted(set(current_keys) - baseline)
    if new:
        detail = "\n  ".join(current_keys[k] for k in new)
        pytest.fail(
            f"{len(new)} new Protocol async-optional dispatch finding(s) (advisory check, "
            f"docstring-phrase matching has real false-positive risk, see module docstring):\n  "
            f"{detail}\n"
            f"Fix the call site (await it directly, or check "
            f"asyncio.iscoroutine()/inspect.isawaitable() before use), or if this is a confirmed "
            f"false positive, refresh the baseline:\n"
            f"  pytest tests/test_meta/test_protocol_async_optional_dispatch.py {_REFRESH_FLAG}"
        )


class TestScanForDispatchViolationsDetectsShapes:
    """Direct regression tests for the checker itself, via synthetic
    source written to a real temp tree (``scan_for_dispatch_violations``
    reads from disk, not from a string)."""

    _ASYNC_OPTIONAL_PROTOCOL = '''
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Widget(Protocol):
    """Async-aware: implementations MAY be async, a coroutine also
    works too, the caller awaits any awaitable result."""

    def fetch(self, n: int) -> int:
        ...
'''

    def _scan(self, tmp_path, protocol_src: str, caller_src: str) -> list[str]:
        (tmp_path / "proto.py").write_text(protocol_src, encoding="utf-8")
        (tmp_path / "caller.py").write_text(caller_src, encoding="utf-8")
        return scan_for_dispatch_violations(tmp_path)

    def test_fires_on_bare_call_without_await_or_check(self, tmp_path):
        caller_src = "def run(widget):\n" "    result = widget.fetch(3)\n" "    return result\n"
        violations = self._scan(tmp_path, self._ASYNC_OPTIONAL_PROTOCOL, caller_src)
        assert len(violations) == 1
        assert "fetch" in violations[0]

    def test_silent_when_directly_awaited(self, tmp_path):
        caller_src = "async def run(widget):\n" "    result = await widget.fetch(3)\n" "    return result\n"
        violations = self._scan(tmp_path, self._ASYNC_OPTIONAL_PROTOCOL, caller_src)
        assert violations == []

    def test_silent_when_iscoroutine_checked(self, tmp_path):
        caller_src = (
            "import asyncio\n"
            "\n"
            "async def run(widget):\n"
            "    maybe = widget.fetch(3)\n"
            "    result = await maybe if asyncio.iscoroutine(maybe) else maybe\n"
            "    return result\n"
        )
        violations = self._scan(tmp_path, self._ASYNC_OPTIONAL_PROTOCOL, caller_src)
        assert violations == []

    def test_silent_when_isawaitable_checked(self, tmp_path):
        caller_src = (
            "import inspect\n"
            "\n"
            "async def run(widget):\n"
            "    maybe = widget.fetch(3)\n"
            "    if inspect.isawaitable(maybe):\n"
            "        maybe = await maybe\n"
            "    return maybe\n"
        )
        violations = self._scan(tmp_path, self._ASYNC_OPTIONAL_PROTOCOL, caller_src)
        assert violations == []

    def test_silent_when_protocol_docstring_does_not_match(self, tmp_path):
        protocol_src = (
            "from __future__ import annotations\n"
            "from typing import Protocol, runtime_checkable\n"
            "\n"
            "@runtime_checkable\n"
            "class Widget(Protocol):\n"
            '    """Plain synchronous contract, nothing async here."""\n'
            "\n"
            "    def fetch(self, n: int) -> int:\n"
            "        ...\n"
        )
        caller_src = "def run(widget):\n" "    return widget.fetch(3)\n"
        violations = self._scan(tmp_path, protocol_src, caller_src)
        assert violations == []

    def test_non_protocol_class_with_matching_docstring_not_collected(self, tmp_path):
        # Same trigger phrases, but the class is a plain class, not a
        # Protocol; the method-name collection step must be gated on
        # the class shape, not just the docstring text.
        protocol_src = (
            "class Widget:\n"
            '    """MAY be async-aware; a coroutine also works too, awaits any awaitable result."""\n'
            "\n"
            "    def fetch(self, n: int) -> int:\n"
            "        ...\n"
        )
        caller_src = "def run(widget):\n" "    return widget.fetch(3)\n"
        violations = self._scan(tmp_path, protocol_src, caller_src)
        assert violations == []

    def test_method_level_docstring_also_matches(self, tmp_path):
        protocol_src = (
            "from __future__ import annotations\n"
            "from typing import Protocol, runtime_checkable\n"
            "\n"
            "@runtime_checkable\n"
            "class Widget(Protocol):\n"
            '    """Structural contract."""\n'
            "\n"
            "    def fetch(self, n: int) -> int:\n"
            '        """Returns n. Coroutine also works too, the caller awaits any awaitable result."""\n'
            "        ...\n"
        )
        caller_src = "def run(widget):\n" "    return widget.fetch(3)\n"
        violations = self._scan(tmp_path, protocol_src, caller_src)
        assert len(violations) == 1
