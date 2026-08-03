"""Every ``continue-on-error: true`` in this repo's CI workflow files must be
an explicitly reviewed, allowlisted advisory step -- never a silent,
un-vetted gate-defeat.

Wires ``py_ci_shared.ci_workflow_gate`` (see that module's docstring for the
full audit rationale: 2026-07-21 O-10/O-11/O-17, where a
``continue-on-error: true`` on a step meant to BLOCK the gate silently
turned it into a no-op -- the job still shows green in the PR check list
even though the gate step itself failed).

As of 2026-07-22 this repo has exactly ONE ``continue-on-error: true``: on
ci.yml's "Run black --check (filtered)" step. That's deliberately advisory
-- the dedicated ``black-filtered.yml`` workflow already blocks on the same
check, so staying advisory here avoids double-failing a PR on one finding
through two separate jobs (see the comment directly above that step in
ci.yml for the full rationale). Any FUTURE ``continue-on-error: true``
added anywhere under ``.github/workflows/``, on any step, in any file,
without also being added to ``_REVIEWED_ADVISORY_STEPS`` below, fails this
test -- forcing a human decision rather than letting a copy-pasted or
refactored gate step silently regain (or lose) blocking status unnoticed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from py_ci_shared.ci_workflow_gate import assert_continue_on_error_is_reviewed, find_continue_on_error_steps

_WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"

# Per-file allowlist of step/job names deliberately kept advisory today.
# Every file under .github/workflows/ must have an entry (an empty set is
# fine, and is the expected value for every file but ci.yml right now) --
# a new workflow file with no entry fails loudly instead of being silently
# skipped by a glob that happens not to see it reviewed.
_REVIEWED_ADVISORY_STEPS: dict[str, set[str]] = {
    "black-filtered.yml": set(),
    "ci.yml": {"Run black --check (filtered)"},
    "mypy-full.yml": set(),
    "release.yml": set(),
}


def _list_workflow_files(workflows_dir: Path) -> list[Path]:
    # GitHub Actions accepts both `.yml` and `.yaml` under .github/workflows/
    # -- a `*.yml`-only glob would silently never even scan a future `.yaml`
    # file, which is a total bypass of this whole check, not just a missed
    # line within a scanned file.
    return sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))


def _find_unmapped_workflow_files(workflows_dir: Path, reviewed_advisory_steps: dict[str, set[str]]) -> list[str]:
    return [p.name for p in _list_workflow_files(workflows_dir) if p.name not in reviewed_advisory_steps]


def _find_stale_allowlist_entries(workflows_dir: Path, reviewed_advisory_steps: dict[str, set[str]]) -> list[str]:
    stale: list[str] = []
    for name, reviewed in reviewed_advisory_steps.items():
        path = workflows_dir / name
        if not path.exists():
            continue  # covered by test_ci_continue_on_error_steps_are_reviewed
        actual = set(find_continue_on_error_steps(path))
        stale.extend(f"{name}: {step!r} is allowlisted but no longer has continue-on-error: true" for step in sorted(reviewed - actual))
    return stale


def test_ci_continue_on_error_steps_are_reviewed():
    workflow_files = _list_workflow_files(_WORKFLOWS_DIR)
    if not workflow_files:
        pytest.fail(f"No workflow files found under {_WORKFLOWS_DIR} -- check the path.")

    unmapped = _find_unmapped_workflow_files(_WORKFLOWS_DIR, _REVIEWED_ADVISORY_STEPS)
    if unmapped:
        pytest.fail(
            f"New workflow file(s) with no _REVIEWED_ADVISORY_STEPS entry in "
            f"tests/test_meta/test_ci_continue_on_error_reviewed.py: {unmapped}. "
            f"Add an entry (empty set if it has no advisory steps)."
        )

    for path in workflow_files:
        assert_continue_on_error_is_reviewed(path, reviewed_advisory_steps=_REVIEWED_ADVISORY_STEPS[path.name])


def test_reviewed_advisory_steps_are_not_stale():
    """Every name in the allowlist must actually appear as a
    continue-on-error step in its file today -- an entry that no longer
    matches anything (the step was renamed or the line removed) is dead
    weight that would silently let a DIFFERENT future step reuse the same
    name unreviewed, so it is flagged rather than left to rot."""
    stale = _find_stale_allowlist_entries(_WORKFLOWS_DIR, _REVIEWED_ADVISORY_STEPS)
    if stale:
        msg = "\n  ".join(stale)
        pytest.fail(f"Stale _REVIEWED_ADVISORY_STEPS entries -- remove them:\n  {msg}")


class TestCiWorkflowGateWiringDetectsShapes:
    """Regression tests for the wiring in this file (not a re-test of
    ``py_ci_shared.ci_workflow_gate`` itself, which has its own unit-test
    suite): synthetic workflow YAML via ``tmp_path`` proves the gate FIRES
    on a genuinely unreviewed advisory step and stays silent once that step
    is named in the allowlist, or is absent altogether."""

    def test_fires_on_new_unreviewed_continue_on_error(self, tmp_path):
        p = tmp_path / "synthetic.yml"
        p.write_text(
            "jobs:\n" "  lint:\n" "    steps:\n" "      - name: Run new-tool\n" "        run: new-tool check .\n" "        continue-on-error: true\n",
            encoding="utf-8",
        )
        with pytest.raises(pytest.fail.Exception, match="Run new-tool"):
            assert_continue_on_error_is_reviewed(p, reviewed_advisory_steps=set())

    def test_silent_once_step_is_named_in_allowlist(self, tmp_path):
        p = tmp_path / "synthetic.yml"
        p.write_text(
            "jobs:\n" "  lint:\n" "    steps:\n" "      - name: Run new-tool\n" "        run: new-tool check .\n" "        continue-on-error: true\n",
            encoding="utf-8",
        )
        assert_continue_on_error_is_reviewed(p, reviewed_advisory_steps={"Run new-tool"})  # must not raise
        assert find_continue_on_error_steps(p) == ["Run new-tool"]

    def test_clean_workflow_with_no_continue_on_error_is_silent(self, tmp_path):
        p = tmp_path / "synthetic.yml"
        p.write_text(
            "jobs:\n" "  build:\n" "    steps:\n" "      - name: Build\n" "        run: make\n",
            encoding="utf-8",
        )
        assert_continue_on_error_is_reviewed(p, reviewed_advisory_steps=set())
        assert find_continue_on_error_steps(p) == []

    def test_stale_allowlist_entry_fires(self, tmp_path):
        """A `_REVIEWED_ADVISORY_STEPS` entry naming a step that no longer
        carries continue-on-error (renamed or removed) must be caught by
        `test_reviewed_advisory_steps_are_not_stale` -- proven here by
        constructing that exact stale shape, not just observing today's
        clean repo has none."""
        p = tmp_path / "synthetic.yml"
        p.write_text(
            "jobs:\n" "  lint:\n" "    steps:\n" "      - name: Build\n" "        run: make\n",
            encoding="utf-8",
        )
        stale = _find_stale_allowlist_entries(tmp_path, {"synthetic.yml": {"Run gone-tool"}})
        assert stale == ["synthetic.yml: 'Run gone-tool' is allowlisted but no longer has continue-on-error: true"]

    def test_stale_check_silent_when_entry_still_matches(self, tmp_path):
        p = tmp_path / "synthetic.yml"
        p.write_text(
            "jobs:\n" "  lint:\n" "    steps:\n" "      - name: Run new-tool\n" "        continue-on-error: true\n",
            encoding="utf-8",
        )
        assert _find_stale_allowlist_entries(tmp_path, {"synthetic.yml": {"Run new-tool"}}) == []

    def test_unmapped_workflow_file_fires(self, tmp_path):
        """A workflow file with no `_REVIEWED_ADVISORY_STEPS` entry must be
        caught by `test_ci_continue_on_error_steps_are_reviewed`'s unmapped
        check -- proven here by constructing that exact shape, not just
        observing today's repo has every file mapped."""
        (tmp_path / "new-workflow.yml").write_text(
            "jobs:\n" "  build:\n" "    steps:\n" "      - name: Build\n" "        run: make\n",
            encoding="utf-8",
        )
        assert _find_unmapped_workflow_files(tmp_path, {}) == ["new-workflow.yml"]

    def test_unmapped_check_silent_when_file_is_mapped(self, tmp_path):
        (tmp_path / "new-workflow.yml").write_text(
            "jobs:\n" "  build:\n" "    steps:\n" "      - name: Build\n" "        run: make\n",
            encoding="utf-8",
        )
        assert _find_unmapped_workflow_files(tmp_path, {"new-workflow.yml": set()}) == []

    def test_yaml_extension_is_also_scanned(self, tmp_path):
        """`.yaml` (not just `.yml`) is a valid GitHub Actions workflow
        extension -- a glob that only looks for `.yml` would silently never
        scan a future `.yaml` file at all."""
        (tmp_path / "new-workflow.yaml").write_text(
            "jobs:\n" "  build:\n" "    steps:\n" "      - name: Build\n" "        run: make\n",
            encoding="utf-8",
        )
        assert [p.name for p in _list_workflow_files(tmp_path)] == ["new-workflow.yaml"]
