"""Regression guard: every git-URL dependency in pyproject.toml stays pinned
to a full commit SHA.

Wires the shared ``py_ci_shared.git_dependency_pins`` check into llm_bench's
own meta-test suite. Generalizes a 2026-07-21 audit finding (S-04): a
``py-ci-shared`` git dependency in a consuming repo floated on the default
branch with no commit pin at all, while a sibling ``pyutilz`` git dependency
in the same file WAS pinned -- and the adjacent comment incorrectly claimed
it mirrored that pattern. llm_bench's own git+ dependency (``py-ci-shared``,
declared under ``[project.optional-dependencies].dev``) is pinned to a full
40-hex-character commit SHA as of this writing; this test exists so any
future edit that drops back to a floating branch/tag ref (or drops the ref
entirely) fails CI immediately instead of silently reintroducing S-04's
exact bug.

See ``py_ci_shared/git_dependency_pins.py`` for the full check
implementation and rationale -- deliberately NOT re-implemented here; a
bespoke scanner would just be a worse copy of the shared one, and this
package already depends on py-ci-shared for other meta-tests
(``test_code_audit_baseline.py``, ``conftest.py``'s baseline-refresh flags).

No baseline/whitelist escape hatch here, unlike the other checkers in this
directory: per ``assert_all_git_dependencies_pinned``'s own docstring, an
unpinned git dependency is unconditionally wrong -- there is no legitimate
"grandfathered" or "false positive" case for a reproducibility guarantee, so
unlike ``test_no_unicode_in_console_output.py``'s ``_WHITELIST`` or
``test_code_audit_baseline.py``'s baseline JSON, adding one here would just
be a way to quietly re-break the guarantee this test exists to enforce.
"""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.git_dependency_pins import assert_all_git_dependencies_pinned, find_unpinned_git_dependencies

_PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_all_git_dependencies_pinned():
    assert_all_git_dependencies_pinned(_PYPROJECT_PATH)


class TestFindUnpinnedGitDependenciesDetectsShapes:
    """Direct regression tests for the shared checker's behavior against
    this repo's own dependency-declaration style (PEP 508 direct
    references inside a ``dependencies = [...]`` TOML array), so a future
    change to the shared helper that silently stops catching llm_bench's
    own dependency shape is caught here too, not just in py-ci-shared's own
    test suite."""

    def _run(self, tmp_path, source: str) -> list[str]:
        p = tmp_path / "synthetic_pyproject.toml"
        p.write_text(source, encoding="utf-8")
        # find_unpinned_git_dependencies is typed list[str] -> list[str] in py_ci_shared, but that
        # package ships no py.typed marker, so mypy sees the imported symbol as untyped/Any here;
        # the explicit annotation below narrows it back instead of leaking Any past this helper.
        violations: list[str] = find_unpinned_git_dependencies(p)
        return violations

    def test_floating_branch_ref_detected(self, tmp_path):
        violations = self._run(
            tmp_path,
            '[project]\ndependencies = [\n    "foopkg @ git+https://github.com/example/foopkg.git@main",\n]\n',
        )
        assert violations == ["main"]

    def test_no_ref_at_all_detected(self, tmp_path):
        violations = self._run(
            tmp_path,
            '[project]\ndependencies = [\n    "foopkg @ git+https://github.com/example/foopkg.git",\n]\n',
        )
        assert violations == ["<no ref>"]

    def test_short_sha_detected(self, tmp_path):
        violations = self._run(
            tmp_path,
            '[project]\ndependencies = [\n    "foopkg @ git+https://github.com/example/foopkg.git@1234567",\n]\n',
        )
        assert violations == ["1234567"]

    def test_full_sha_pin_not_flagged(self, tmp_path):
        violations = self._run(
            tmp_path,
            '[project]\ndependencies = [\n    "foopkg @ git+https://github.com/example/foopkg.git@' 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",\n]\n',
        )
        assert violations == []

    def test_no_git_dependencies_not_flagged(self, tmp_path):
        violations = self._run(tmp_path, '[project]\ndependencies = [\n    "httpx>=0.25",\n]\n')
        assert violations == []
