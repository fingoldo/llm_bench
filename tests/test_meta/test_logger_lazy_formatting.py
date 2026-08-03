"""Forbid f-string args to ``logger.<level>()`` calls under src/llm_bench/.

``logger.info(f"x={x}")`` builds the string EVEN WHEN the log level is
disabled — wasted CPU on hot paths. The lazy form
``logger.info("x=%s", x)`` defers formatting to the handler, which
no-ops when the level is disabled.

This matters for the runner's per-call logging (one call per LLM
invocation, hundreds per round); f-strings there add real overhead.

Whitelist via ``# noqa: G004`` comment on the line for legitimate cases
(complex conditional formatting where %s isn't ergonomic).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench"

_LOGGER_METHODS = frozenset({"debug", "info", "warning", "error", "critical", "exception"})


def _audit_file(path: Path, rel: str) -> list[str]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [f"{rel}: SyntaxError: {e}"]
    lines = src.splitlines()

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _LOGGER_METHODS:
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if not isinstance(first_arg, ast.JoinedStr):
            continue  # plain string or %s template — fine
        # Skip if line carries a noqa: G004
        ln = node.lineno
        if ln <= len(lines) and "noqa: G004" in lines[ln - 1]:
            continue
        violations.append(
            f"{rel}:{node.lineno}: f-string passed to logger.{node.func.attr}() — "
            f"use lazy formatting `logger.{node.func.attr}('msg %s', x)` "
            f"or add `# noqa: G004` if intentional."
        )
    return violations


def test_no_fstring_in_logger_calls():
    violations: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(_SRC_ROOT))
        violations.extend(_audit_file(path, rel))
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"Eager logger formatting (f-string args):\n  {msg}")
