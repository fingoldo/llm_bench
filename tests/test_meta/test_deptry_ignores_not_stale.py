"""Meta-test: every [tool.deptry.per_rule_ignores] entry must name a currently-declared
dependency (DEP002) or the package's own distribution name (DEP003).

Ported from mlframe's tests/test_meta/test_deptry_ignores_not_stale.py (no shared helper for
this exists yet in py-ci-shared). deptry's DEP002/DEP003 ignores in pyproject.toml have no
built-in expiry mechanism: an ignore entry added for a real dependency stays in the list
forever, even after that dependency is later removed from [project.dependencies]/
[project.optional-dependencies]/[build-system.requires] -- the per_rule_ignores list only
grows, never self-prunes, and deptry itself has no "is this ignore still load-bearing" check.

This test covers DEP002 and DEP003 specifically, the two rules llm_bench's own
[tool.deptry.per_rule_ignores] block currently uses (see pyproject.toml's inline comments for
per-entry rationale -- httpx/tenacity/pydantic pin llm_bench's own floor for packages pyutilz
uses internally; ruff/black/mypy/pytest*/hypothesis/pre-commit/detect-secrets/py-ci-shared are
dev-tool CLIs never `import`-ed by name; DEP003 'llm_bench' is the package's own self-import,
misclassified as a transitive dependency under an editable install):
- DEP002 (declared but not directly imported): each named package, BY DEFINITION of what
  DEP002 means, should still be traceable to a real declaration somewhere in
  [project.dependencies]/[project.optional-dependencies]/[build-system.requires] -- an ignore
  for a package that's been removed from every declaration is a dead ignore nobody would
  notice went stale otherwise.
- DEP003 (transitive dependency imported directly): llm_bench only ever ignores this rule for
  its own self-import (see pyproject.toml comment above), so each entry must normalize to the
  project's own distribution name (``[project].name``) -- anything else would mean a REAL
  transitive-dependency ignore snuck in without the accompanying rationale this file's own
  DEP002 check exists to protect.

DEP001 (import missing from declared deps) is NOT checked here: DEP001 entries name *import*
names for packages deliberately NOT declared in this file at all, or local test-helper modules
-- there's no declared-dependency list to cross-check them against. llm_bench's own pyproject.toml
carries no DEP001 ignores today (the one true DEP001 finding, orjson, was fixed by declaring it
rather than ignored -- see the [tool.deptry] comment block).
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Known first-party / non-PyPI exceptions: not declared as a normal dependency, but documented
# in pyproject.toml's own [tool.deptry.per_rule_ignores] comment block as to why the ignore is
# real anyway. Empty today (every current llm_bench DEP002/DEP003 entry resolves cleanly to a
# real declaration) -- kept as the escape hatch for the next confirmed false positive.
_WHITELIST: set[str] = set()

_SPEC_RE = re.compile(r"^[A-Za-z0-9_.\-]+")


def _normalize(name: str) -> str:
    """Returns ``name.lower().replace('_', '-')``."""
    return name.lower().replace("_", "-")


def _declared_dependency_names(data: dict) -> set[str]:
    """Every package name declared anywhere in a parsed pyproject.toml: [project.dependencies],
    every [project.optional-dependencies] group, and [build-system.requires]."""
    names: set[str] = set()

    def _add_all(specs):
        """Test helper: for spec in specs: m = _SPEC_RE.match(spec) if m: names.a...."""
        for spec in specs:
            m = _SPEC_RE.match(spec)
            if m:
                names.add(_normalize(m.group(0)))

    _add_all(data.get("project", {}).get("dependencies", []))
    for group_specs in data.get("project", {}).get("optional-dependencies", {}).values():
        _add_all(group_specs)
    _add_all(data.get("build-system", {}).get("requires", []))
    return names


def _stale_deptry_ignores(data: dict) -> list[str]:
    """Cross-references a parsed pyproject.toml's [tool.deptry.per_rule_ignores] DEP002/DEP003
    entries against its own declared dependencies (DEP002) or its own distribution name
    (DEP003). Returns a list of violation strings; empty means every ignore still resolves."""
    declared = _declared_dependency_names(data)
    project_name = _normalize(data.get("project", {}).get("name", ""))
    per_rule_ignores = data.get("tool", {}).get("deptry", {}).get("per_rule_ignores", {})

    violations: list[str] = []

    for pkg in per_rule_ignores.get("DEP002", []):
        norm = _normalize(pkg)
        if norm not in declared and pkg not in _WHITELIST:
            violations.append(
                f"DEP002: {pkg!r} (normalized {norm!r}) is not declared anywhere in "
                f"[project.dependencies]/[project.optional-dependencies]/[build-system.requires] "
                f"and is not a _WHITELIST entry"
            )

    for pkg in per_rule_ignores.get("DEP003", []):
        norm = _normalize(pkg)
        if norm != project_name and pkg not in _WHITELIST:
            violations.append(
                f"DEP003: {pkg!r} (normalized {norm!r}) does not match the project's own " f"distribution name {project_name!r} and is not a _WHITELIST entry"
            )

    return violations


def _stale_ignore_violations(pyproject_path: Path) -> list[str]:
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    return _stale_deptry_ignores(data)


def test_deptry_ignores_not_stale():
    """Every DEP002/DEP003 per_rule_ignores entry in llm_bench's own pyproject.toml must still
    resolve (or be a documented _WHITELIST exception) -- otherwise it's a dead ignore nobody
    would notice went stale, per this file's module docstring."""
    violations = _stale_ignore_violations(REPO_ROOT / "pyproject.toml")
    if violations:
        pytest.fail(
            "Stale deptry per_rule_ignores entries found in pyproject.toml:\n  "
            + "\n  ".join(violations)
            + "\nEither the dependency/self-reference was genuinely removed (delete the ignore "
            "entry too) or it's a real first-party/non-PyPI exception (add it to _WHITELIST "
            "with a comment)."
        )


class TestStaleDeptryIgnoresDetectsShapes:
    """Direct regression tests for the checker itself, against synthetic pyproject.toml files
    written to tmp_path (never against the real repo file, per this suite's convention)."""

    def _run(self, tmp_path, toml_text: str) -> list[str]:
        p = tmp_path / "pyproject.toml"
        p.write_text(toml_text, encoding="utf-8")
        return _stale_ignore_violations(p)

    def test_dep002_entry_for_removed_dependency_detected(self, tmp_path):
        # "httpx" is declared, "long-gone-pkg" is not -- only the latter should be flagged.
        violations = self._run(
            tmp_path,
            """
[project]
name = "demo-pkg"
dependencies = ["httpx>=0.25"]

[tool.deptry.per_rule_ignores]
DEP002 = ["httpx", "long-gone-pkg"]
""",
        )
        assert len(violations) == 1
        assert "long-gone-pkg" in violations[0]
        assert "httpx" not in violations[0]

    def test_dep003_entry_not_matching_project_name_detected(self, tmp_path):
        violations = self._run(
            tmp_path,
            """
[project]
name = "demo-pkg"
dependencies = []

[tool.deptry.per_rule_ignores]
DEP003 = ["demo_pkg", "some-other-package"]
""",
        )
        assert len(violations) == 1
        assert "some-other-package" in violations[0]

    def test_clean_ignores_not_flagged(self, tmp_path):
        # Mirrors llm_bench's own shape: DEP002 entries all declared (across dependencies,
        # an optional-dependencies group, and build-system.requires), DEP003 is the self-import,
        # normalized underscore/hyphen forms both resolve correctly.
        violations = self._run(
            tmp_path,
            """
[build-system]
requires = ["setuptools>=61.0"]

[project]
name = "demo-pkg"
dependencies = ["httpx>=0.25", "pydantic>=2.0"]

[project.optional-dependencies]
dev = ["pytest>=7.0"]

[tool.deptry.per_rule_ignores]
DEP002 = ["httpx", "pydantic", "pytest", "setuptools"]
DEP003 = ["demo_pkg"]
""",
        )
        assert violations == []

    def test_whitelist_entry_suppresses_violation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys.modules[__name__], "_WHITELIST", {"vendored-only-tool"})
        violations = self._run(
            tmp_path,
            """
[project]
name = "demo-pkg"
dependencies = []

[tool.deptry.per_rule_ignores]
DEP002 = ["vendored-only-tool"]
""",
        )
        assert violations == []
