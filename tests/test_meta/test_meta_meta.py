"""Police the police: the meta-test suite itself follows actionable
failure-message + private-import discipline.

Two guarantees enforced:

  M1. Every ``pytest.fail(...)`` call inside ``tests/test_meta/`` carries
      an actionable message (a colon, slash, angle bracket, or one of
      a small set of fix-prompt verbs). Generic ``pytest.fail("broken")``
      is rejected — a meta-test failure should tell the reviewer what
      to do, not just that something is wrong.

  M2. Meta-tests do NOT import private symbols (names starting with
      ``_``) from the production package. The whole point of a meta-test
      is to police the public contract; reaching into internals tests
      implementation, not behaviour. Whitelist via
      ``_PERMITTED_PRIVATE_IMPORTS`` for legitimate cases.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_TEST_META_DIR = Path(__file__).resolve().parent

# Words / characters that count a failure message as actionable.
_ACTIONABLE_RE = re.compile(
    r"[:/<>]|\b(Add|Either|Refresh|Whitelist|Fix|Run|Update|Remove|Document|" r"Restore|Replace|Move|Use)\b",
    re.IGNORECASE,
)

# Private symbols a meta-test is allowed to touch. Format:
# "test_meta_filename::imported_dotted_name".
_PERMITTED_PRIVATE_IMPORTS: set[str] = set()


def _meta_test_files() -> list[Path]:
    """All test_*.py files in tests/test_meta/, except this file."""
    out = []
    for py in _TEST_META_DIR.glob("test_*.py"):
        if py.name == Path(__file__).name:
            continue
        out.append(py)
    return sorted(out)


def _walk_pytest_fail_calls(tree: ast.AST):
    """Yield every ``pytest.fail(...)`` Call node."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "fail":
            if isinstance(f.value, ast.Name) and f.value.id == "pytest":
                yield node


def _arg_static_text(arg: ast.AST) -> tuple[str, bool]:
    """Return ``(joined_static_text, has_dynamic)`` for a Call's first arg."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value, False
    if isinstance(arg, ast.JoinedStr):
        # f-string — concat string parts; the {expr} parts are dynamic.
        static = "".join(part.value for part in arg.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
        has_dynamic = any(not (isinstance(part, ast.Constant) and isinstance(part.value, str)) for part in arg.values)
        return static, has_dynamic
    if isinstance(arg, ast.BinOp):
        # "..." + var or "%s" % var
        l, dl = _arg_static_text(arg.left)
        r, dr = _arg_static_text(arg.right)
        return l + r, dl or dr or True
    return "", True


def test_pytest_fail_messages_are_actionable():
    """M1 — every pytest.fail() must include actionable detail."""
    violations: list[str] = []
    for path in _meta_test_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:
            violations.append(f"{path.name}: SyntaxError: {e}")
            continue
        for call in _walk_pytest_fail_calls(tree):
            if not call.args:
                violations.append(f"{path.name}:{call.lineno}: pytest.fail() with no message — " f"add an actionable hint.")
                continue
            text, has_dynamic = _arg_static_text(call.args[0])
            # Dynamic parts (f-string expressions) likely include the offending
            # value, so they ARE actionable. Static-only strings are audited.
            if has_dynamic:
                continue
            if not _ACTIONABLE_RE.search(text):
                violations.append(
                    f"{path.name}:{call.lineno}: pytest.fail({text!r}) is "
                    f"not actionable — include a colon, path, angle bracket, "
                    f"or fix-prompt verb (Add/Either/Refresh/Fix/Run/Update/...)."
                )
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"Non-actionable pytest.fail() calls:\n  {msg}")


def test_no_private_imports_from_production():
    """M2 — meta-tests don't reach into private internals."""
    violations: list[str] = []
    for path in _meta_test_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:
            violations.append(f"{path.name}: SyntaxError: {e}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not mod.startswith("llm_bench"):
                    continue
                for alias in node.names:
                    if alias.name.startswith("_"):
                        key = f"{path.stem}::{mod}.{alias.name}"
                        if key in _PERMITTED_PRIVATE_IMPORTS:
                            continue
                        violations.append(
                            f"{path.name}:{node.lineno}: imports private "
                            f"{alias.name!r} from {mod!r}. Either whitelist "
                            f"in _PERMITTED_PRIVATE_IMPORTS with rationale, "
                            f"or test the public contract instead."
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if parts[0] == "llm_bench" and any(p.startswith("_") for p in parts):
                        violations.append(
                            f"{path.name}:{node.lineno}: imports private "
                            f"module path {alias.name!r}. Use the public "
                            f"surface or whitelist with rationale."
                        )
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"Meta-tests reaching into private internals:\n  {msg}")
