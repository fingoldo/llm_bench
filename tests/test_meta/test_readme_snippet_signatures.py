"""Verify README.md's ```python code snippets call the real llm_bench API
with every REQUIRED parameter supplied.

Motivation: the Quick Start snippet drifted from the real API twice at
once - ``Stage(...)`` was missing its required ``op=`` kwarg, and
``run_phase(...)`` was missing its required ``candidates=`` kwarg (the
value had been misplaced onto ``Benchmark(...)`` instead). Both were
silent TypeErrors that only a human running the snippet would catch.
This test extracts every fenced ```python block from README.md,
ast.parses it (never executes it - snippets reference undefined
placeholder names like ``my_pool`` that aren't meant to run standalone),
resolves each snippet's own ``from llm_bench import ...`` /
``import llm_bench`` lines to real objects via importlib, and for every
call whose callee resolves to one of those real objects, diffs
inspect.signature(...) against the call's actual args/keywords.

Mechanical signature diff -> low false-positive risk, so this is a hard
assert-empty check (no baseline ratchet needed, unlike
test_code_audit_baseline.py's heuristic checks).

Whitelist confirmed false positives (e.g. a snippet intentionally
demonstrating a **kwargs-forwarding call the checker can't fully
resolve) via ``_WHITELIST`` below.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

_README_PATH = Path(__file__).resolve().parents[2] / "README.md"

# Per-snippet whitelist: "<label>::<readme_lineno>" entries skipped.
_WHITELIST: set[str] = set()

_FENCE_OPEN_PREFIX = "```python"
_FENCE_CLOSE = "```"


def _extract_python_blocks(markdown_text: str) -> list[tuple[int, str]]:
    """Return ``(first_code_line_1indexed, code)`` for every fenced
    ```python block in ``markdown_text``. Line numbers are relative to
    the markdown file so violation messages can point back at README.md."""
    lines = markdown_text.splitlines()
    blocks: list[tuple[int, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip().startswith(_FENCE_OPEN_PREFIX):
            start_line = i + 2  # 1-indexed: first line AFTER the fence
            body: list[str] = []
            i += 1
            while i < n and lines[i].strip() != _FENCE_CLOSE:
                body.append(lines[i])
                i += 1
            blocks.append((start_line, "\n".join(body)))
        i += 1
    return blocks


def _collect_imports(tree: ast.Module) -> tuple[dict[str, Any], list[str]]:
    """Resolve this snippet's own ``from llm_bench... import ...`` /
    ``import llm_bench...`` lines to real objects via importlib.
    Non-llm_bench imports are ignored (out of scope for this checker).
    Returns (name -> resolved object, [unresolvable-import errors])."""
    imported: dict[str, Any] = {}
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod_name = node.module
            if not mod_name or not (mod_name == "llm_bench" or mod_name.startswith("llm_bench.")):
                continue
            try:
                mod = importlib.import_module(mod_name)
            except ImportError as e:
                errors.append(f"cannot import module {mod_name!r}: {e}")
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if not hasattr(mod, alias.name):
                    errors.append(f"{mod_name}.{alias.name} does not exist (README imports it)")
                    continue
                imported[alias.asname or alias.name] = getattr(mod, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                dotted = alias.name
                if not (dotted == "llm_bench" or dotted.startswith("llm_bench.")):
                    continue
                try:
                    importlib.import_module(dotted)
                except ImportError as e:
                    errors.append(f"cannot import module {dotted!r}: {e}")
                    continue
                top = dotted.split(".", 1)[0]
                bound = alias.asname or top
                imported[bound] = importlib.import_module(dotted) if alias.asname else importlib.import_module(top)
    return imported, errors


def _resolve_callee(func: ast.expr, imported: dict[str, Any], instances: dict[str, type]) -> tuple[str, Any] | None:
    """Resolve a call's callee expression to a real object, IF it chains
    back to something the snippet imported from llm_bench (or, for
    ``obj.method(...)`` calls, to a class an earlier ``obj = Cls(...)``
    assignment tied ``obj`` to). Returns (kind, object) where kind is
    "plain" for a directly-called class/function, or "instance_method"
    for an unbound method reached via a tracked instance (its ``self``
    parameter must be dropped before diffing). None = unresolvable,
    which is not itself a violation - just out of this checker's scope."""
    if isinstance(func, ast.Name):
        name_obj = imported.get(func.id)
        return ("plain", name_obj) if name_obj is not None else None
    if isinstance(func, ast.Attribute):
        attrs: list[str] = []
        cur: ast.expr = func
        while isinstance(cur, ast.Attribute):
            attrs.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            return None
        attrs.reverse()
        root = cur.id
        if root in instances:
            attr_obj: Any = instances[root]
            for attr in attrs:
                attr_obj = getattr(attr_obj, attr, None)
                if attr_obj is None:
                    return None
            return ("instance_method", attr_obj)
        if root in imported:
            attr_obj = imported[root]
            for attr in attrs:
                attr_obj = getattr(attr_obj, attr, None)
                if attr_obj is None:
                    return None
            return ("plain", attr_obj)
        return None
    return None


def _collect_instance_types(tree: ast.Module, imported: dict[str, Any]) -> dict[str, type]:
    """Track ``name = SomeImportedClass(...)`` assignments so later
    ``name.method(...)`` calls (e.g. ``bench.run_phase(...)`` after
    ``bench = Benchmark(...)``) can be resolved too - this is the exact
    shape of the historical ``candidates=`` bug (misplaced onto the
    constructor instead of the method it belonged on)."""
    instances: dict[str, type] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        resolved = _resolve_callee(node.value.func, imported, {})
        if resolved is None:
            continue
        kind, target = resolved
        if kind == "plain" and inspect.isclass(target):
            instances[node.targets[0].id] = target
    return instances


def _callee_repr(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_callee_repr(func.value)}.{func.attr}"
    return "<call>"


def _missing_required_params(sig: inspect.Signature, call: ast.Call) -> list[str]:
    """Diff a resolved signature against a call site's actual args and
    keywords. Returns the names of REQUIRED (no-default) parameters that
    aren't supplied either positionally or by keyword. A call that itself
    uses ``*args``/``**kwargs`` unpacking is treated as "might supply it"
    (can't know the unpacked contents statically) so it's never flagged -
    this keeps the checker's false-positive rate at zero."""
    positional_count = 0
    has_star_args = False
    for a in call.args:
        if isinstance(a, ast.Starred):
            has_star_args = True
        else:
            positional_count += 1

    keyword_names: set[str] = set()
    has_star_kwargs = False
    for kw in call.keywords:
        if kw.arg is None:
            has_star_kwargs = True
        else:
            keyword_names.add(kw.arg)

    missing: list[str] = []
    pos_index = 0
    for param in sig.parameters.values():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        required = param.default is inspect.Parameter.empty

        if param.kind == inspect.Parameter.POSITIONAL_ONLY:
            satisfied = pos_index < positional_count or has_star_args
            pos_index += 1
        elif param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
            satisfied = pos_index < positional_count or has_star_args or param.name in keyword_names or has_star_kwargs
            pos_index += 1
        else:  # KEYWORD_ONLY
            satisfied = param.name in keyword_names or has_star_kwargs

        if required and not satisfied:
            missing.append(param.name)
    return missing


def _scan_snippet(start_line: int, source: str, label: str) -> list[str]:
    violations: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        violations.append(f"{label} (README.md around line {start_line}): SyntaxError: {e}")
        return violations

    imported, import_errors = _collect_imports(tree)
    violations.extend(f"{label} (README.md around line {start_line}): {err}" for err in import_errors)

    instances = _collect_instance_types(tree, imported)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _resolve_callee(node.func, imported, instances)
        if resolved is None:
            continue
        kind, target = resolved
        if not callable(target):
            continue
        try:
            sig = inspect.signature(target)
        except (TypeError, ValueError):
            continue  # builtins / C-implemented callables with no introspectable signature

        if kind == "instance_method":
            params = list(sig.parameters.values())
            if params and params[0].name == "self":
                sig = sig.replace(parameters=params[1:])

        missing = _missing_required_params(sig, node)
        if not missing:
            continue

        lineno = start_line + node.lineno - 1
        key = f"{label}::{lineno}"
        if key in _WHITELIST:
            continue
        violations.append(
            f"{label} (README.md:{lineno}): {_callee_repr(node.func)}(...) is missing "
            f"required argument(s) {missing} - current signature is "
            f"{getattr(target, '__qualname__', target)}{sig}"
        )
    return violations


def _scan_markdown_file(path: Path, rel: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    violations: list[str] = []
    for idx, (start_line, code) in enumerate(_extract_python_blocks(text), start=1):
        violations.extend(_scan_snippet(start_line, code, f"{rel} snippet #{idx}"))
    return violations


def test_readme_snippets_match_real_signatures():
    violations = _scan_markdown_file(_README_PATH, "README.md")
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"README.md code snippet(s) call llm_bench API with a missing required argument (signature drift):\n  {msg}")


class TestScanMarkdownFileDetectsShapes:
    """Direct regression tests for the checker itself, via synthetic
    README-shaped files written to tmp_path (mirrors
    test_no_unicode_in_console_output.py's ``_run`` convention). Reaches
    into the REAL llm_bench package (that's the whole point - the checker
    diffs against live signatures, not a mock), so these use real
    llm_bench classes as the resolution targets."""

    def _run(self, tmp_path, markdown: str) -> list[str]:
        p = tmp_path / "synthetic_README.md"
        p.write_text(markdown, encoding="utf-8")
        return _scan_markdown_file(p, "synthetic_README.md")

    def test_direct_call_missing_required_kwarg_detected(self, tmp_path):
        # Stage requires id, op, prompt_builder, parser - only id given.
        md = "```python\nfrom llm_bench import Stage\nStage(id='draft')\n```\n"
        violations = self._run(tmp_path, md)
        assert len(violations) == 1
        assert "op" in violations[0] and "prompt_builder" in violations[0] and "parser" in violations[0]

    def test_instance_method_missing_required_kwarg_detected(self, tmp_path):
        # Replicates the actual historical bug shape: candidates= belongs
        # on run_phase(...), not on the Benchmark(...) constructor call.
        md = (
            "```python\n"
            "from llm_bench import Benchmark, Stage, StageGraph\n"
            "from llm_bench.storage import FileStorage\n"
            "bench = Benchmark(task_pool=None, stages=StageGraph([]), storage=FileStorage(root='.'))\n"
            "report = bench.run_phase(tag='exp_v1')\n"
            "```\n"
        )
        violations = self._run(tmp_path, md)
        assert len(violations) == 1
        assert "bench.run_phase" in violations[0]
        assert "candidates" in violations[0]

    def test_unresolvable_import_detected(self, tmp_path):
        md = "```python\nfrom llm_bench import ThisNameDoesNotExist\n```\n"
        violations = self._run(tmp_path, md)
        assert len(violations) == 1
        assert "ThisNameDoesNotExist" in violations[0]

    def test_clean_snippet_not_flagged(self, tmp_path):
        md = (
            "```python\n"
            "from llm_bench import Benchmark, Stage, StageGraph\n"
            "from llm_bench.storage import FileStorage\n"
            "bench = Benchmark(task_pool=None, stages=StageGraph([Stage(id='d', op='d', prompt_builder=None, parser=None)]), storage=FileStorage(root='.'))\n"
            "report = bench.run_phase(tag='exp_v1', candidates=['m1'])\n"
            "```\n"
        )
        violations = self._run(tmp_path, md)
        assert violations == []

    def test_calls_to_unimported_names_ignored(self, tmp_path):
        # print(...) and a bare undefined-name call (my_helper) are
        # correctly out of scope - the checker never resolves them, so
        # they neither crash nor false-positive.
        md = "```python\nprint('hello')\nmy_helper(1, 2, 3)\n```\n"
        violations = self._run(tmp_path, md)
        assert violations == []
