"""Catch a blocking local pre-commit hook that has no CI equivalent at all.

Motivating audit finding (10-High, closed 2026-07-22): six pre-commit hooks
explicitly named ``*-blocking`` (vulture-blocking, interrogate-blocking,
deptry-blocking, codespell-blocking, yamllint-blocking, zizmor-blocking) had
ZERO equivalent in any CI workflow. Only a local ``pre-commit`` hook enforced
them, and ``git commit --no-verify`` (or any PR opened without pre-commit
installed locally, e.g. from GitHub's web UI) shipped through every other
green CI check with none of the six ever having run. A ``hygiene`` job
running all six was added to ``.github/workflows/ci.yml`` the same session
to close the gap -- this test is the permanent regression guard against that
gap reopening (e.g. a new ``*-blocking`` hook added to
``.pre-commit-config.yaml`` without a matching CI step, or an existing CI
step quietly downgraded to ``continue-on-error: true``).

This is complementary to (not a duplicate of) a separate ``ci_workflow_gate``
check elsewhere in this suite: that one verifies every ``continue-on-error``
CI step is explicitly reviewed/justified; this one verifies every *blocking
local hook* has ANY CI equivalent at all, reviewed or not.

A hook counts as "blocking" if either:
  - its pre-commit ``id`` ends in ``-blocking`` (this project's own naming
    convention for the local, blocking, hooks -- see
    ``.pre-commit-config.yaml``'s header comment), or
  - the repo-level comment block immediately above its ``- repo:`` entry
    contains the literal (case-sensitive) word ``Blocking`` -- catches a
    future hook that is described as blocking in prose but doesn't happen to
    follow the ``-blocking`` id suffix convention.

The comment check matches the whole word ``blocking`` case-insensitively (so
it catches both this repo's ``Blocking --`` style and its ``BLOCKING.``
style, e.g. the real ``mypy-blocking`` hook's own comment), but excludes the
referential phrase ``the blocking ...`` (not preceded by a word boundary
other than "the "): this file's own prose uses lowercase "blocking" as an
ordinary adjective in at least one place that is NOT a self-declaration
("...surfaces everything the blocking gate's --ignore C901 hides", in the
``lint-advisory-warn`` hook's comment, describing a DIFFERENT hook) -- a bare
case-insensitive substring match would misfire on exactly that sentence. See
``TestFindViolationsDetectsShapes.test_lowercase_blocking_word_in_comment_not_falsely_flagged``
and ``test_allcaps_blocking_word_in_comment_is_flagged`` below for the direct
regression tests of both the false-positive and false-negative shapes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

# Matches the standalone word "blocking" in any capitalization ("Blocking",
# "BLOCKING", "blocking"), but not when immediately preceded by "the " --
# that phrasing ("the blocking gate's...") refers to a DIFFERENT hook by
# adjective, not a self-declaration that THIS hook is blocking.
_BLOCKING_COMMENT_RE = re.compile(r"(?<!the )\bblocking\b", re.IGNORECASE)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / ".pre-commit-config.yaml"
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

# Hook ids that are genuinely local-only BY DESIGN, never meant to get a CI
# equivalent, even if a future edit to their id or comment would otherwise
# make the heuristic above pick them up. Each entry documents why. (Today
# neither of these is actually selected as "blocking" by the heuristic --
# their ids don't end in "-blocking" and their comments don't say
# "Blocking" -- this allowlist is a deliberate belt-and-suspenders guard so
# a future prose tweak to their comments can't silently start failing this
# test for a hook the maintainers have already decided not to mirror in CI.)
_LOCAL_ONLY_ALLOWLIST: dict[str, str] = {
    # Third-party repo hook (Yelp/detect-secrets), not this project's own
    # "local" entry. Per ci.yml's `hygiene` job comment: could not be
    # verified to run cleanly against CI's Python during the 2026-07-22 fix
    # pass, so left a known, deliberate gap rather than wired in unverified.
    "detect-secrets": "third-party repo hook; CI parity deliberately deferred, see ci.yml hygiene job comment",
    # Third-party repo hook (Mateusz-Grzelinski/actionlint-py), not this
    # project's own "local" entry. Per ci.yml's `hygiene` job comment:
    # deliberately left pre-commit-only alongside detect-secrets.
    "actionlint": "third-party repo hook; CI parity deliberately deferred, see ci.yml hygiene job comment",
}

# Small alias table: base tool name (hook id with the "-blocking" suffix
# stripped, or the bare id when the hook was flagged via its repo comment
# instead) -> substrings to search for, case-insensitively, in CI job/step
# name + run + uses text. Falls back to the base name itself when absent.
_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "mypy": ("mypy",),
    "bandit": ("bandit",),
    "vulture": ("vulture",),
    "interrogate": ("interrogate",),
    "deptry": ("deptry",),
    "codespell": ("codespell",),
    "yamllint": ("yamllint",),
    "zizmor": ("zizmor",),
    "ruff": ("ruff",),
    "black": ("black",),
    "actionlint": ("actionlint",),
}


@dataclass(frozen=True)
class _CIStep:
    """One CI job or step, flattened to the text we fuzzy-match against."""

    workflow: str
    job: str
    text: str  # lowercased name + run + uses, the fuzzy-match search space
    continue_on_error: bool


def _repo_comment_blocks(raw: str) -> list[str]:
    """Return, in document order, the raw comment block immediately above
    each top-level ``  - repo:`` entry in a pre-commit config (empty string
    if a given entry has no directly-preceding comment lines)."""
    lines = raw.splitlines()
    repo_line_idxs = [i for i, line in enumerate(lines) if line.startswith("  - repo:")]
    blocks: list[str] = []
    for idx in repo_line_idxs:
        comment_lines: list[str] = []
        j = idx - 1
        while j >= 0 and lines[j].strip().startswith("#"):
            comment_lines.append(lines[j].strip())
            j -= 1
        comment_lines.reverse()
        blocks.append("\n".join(comment_lines))
    return blocks


def _blocking_hooks(config_path: Path) -> list[tuple[str, str]]:
    """Return (hook_id, reason) for every hook this project treats as a
    blocking local gate -- see module docstring for the two detection
    signals (id suffix / repo-comment) and the allowlist."""
    raw = config_path.read_text(encoding="utf-8")
    doc: dict[str, Any] = yaml.safe_load(raw) or {}
    repos: list[dict[str, Any]] = doc.get("repos", []) or []
    comment_blocks = _repo_comment_blocks(raw)

    result: list[tuple[str, str]] = []
    for repo, comment in zip(repos, comment_blocks, strict=True):
        comment_flagged = bool(_BLOCKING_COMMENT_RE.search(comment))
        for hook in repo.get("hooks", []) or []:
            hook_id = str(hook.get("id", ""))
            if not hook_id or hook_id in _LOCAL_ONLY_ALLOWLIST:
                continue
            if hook_id.endswith("-blocking"):
                result.append((hook_id, "id suffix"))
            elif comment_flagged:
                result.append((hook_id, "repo-comment"))
    return result


def _base_tool_name(hook_id: str) -> str:
    if hook_id.endswith("-blocking"):
        return hook_id[: -len("-blocking")]
    return hook_id


def _aliases_for(base: str) -> tuple[str, ...]:
    return _TOOL_ALIASES.get(base, (base,))


def _collect_ci_steps(workflows_dir: Path) -> list[_CIStep]:
    """Flatten every job (and, within it, every step) of every workflow
    file into searchable text. Jobs that call a reusable workflow via a
    job-level ``uses:`` instead of a ``steps:`` list (this repo's
    mypy-full.yml / black-filtered.yml) are captured as a single pseudo-step
    keyed on the job's own name + uses path."""
    steps: list[_CIStep] = []
    if not workflows_dir.is_dir():
        return steps
    for wf_path in sorted(workflows_dir.glob("*.yml")):
        doc: dict[str, Any] = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
        jobs: dict[str, Any] = doc.get("jobs", {}) or {}
        for job_id, job_def in jobs.items():
            if not isinstance(job_def, dict):
                continue
            job_name = str(job_def.get("name", job_id))
            job_text = " ".join([job_name, str(job_def.get("uses", ""))]).lower()
            steps.append(
                _CIStep(
                    workflow=wf_path.name,
                    job=str(job_id),
                    text=job_text,
                    continue_on_error=bool(job_def.get("continue-on-error", False)),
                )
            )
            for step in job_def.get("steps", []) or []:
                if not isinstance(step, dict):
                    continue
                step_text = " ".join([str(step.get("name", "")), str(step.get("run", "")), str(step.get("uses", ""))]).lower()
                steps.append(
                    _CIStep(
                        workflow=wf_path.name,
                        job=str(job_id),
                        text=step_text,
                        continue_on_error=bool(step.get("continue-on-error", False)),
                    )
                )
    return steps


def _find_violations(config_path: Path, workflows_dir: Path) -> list[str]:
    """Pure scan: blocking pre-commit hooks with no real CI equivalent."""
    violations: list[str] = []
    ci_steps = _collect_ci_steps(workflows_dir)
    for hook_id, reason in _blocking_hooks(config_path):
        aliases = _aliases_for(_base_tool_name(hook_id))
        matches = [s for s in ci_steps if any(alias in s.text for alias in aliases)]
        if not matches:
            violations.append(
                f"{hook_id}: blocking pre-commit hook (flagged via {reason}) has NO matching "
                f"CI step -- searched job/step name+run+uses text in {workflows_dir}/*.yml for "
                f"any of {list(aliases)!r}. A `git commit --no-verify` or a PR opened without "
                f"pre-commit installed locally skips this check entirely."
            )
        elif all(m.continue_on_error for m in matches):
            locs = ", ".join(f"{m.workflow}:{m.job}" for m in matches)
            violations.append(
                f"{hook_id}: blocking pre-commit hook (flagged via {reason}) only matches CI "
                f"step(s) marked continue-on-error: true ({locs}) -- that is not a real gate, "
                f"a failure there is silently ignored."
            )
    return violations


def test_blocking_precommit_hooks_have_ci_equivalent():
    violations = _find_violations(_CONFIG_PATH, _WORKFLOWS_DIR)
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"Blocking pre-commit hook(s) with no CI parity:\n  {msg}")


class TestFindViolationsDetectsShapes:
    """Direct regression tests for the checker itself, against synthetic
    ``.pre-commit-config.yaml`` / ``.github/workflows/*.yml`` trees built
    under ``tmp_path`` (mirrors ``test_no_unicode_in_console_output.py``'s
    ``TestAuditFileDetectsAllThreeShapes`` convention)."""

    def _write_tree(self, tmp_path: Path, precommit_yaml: str, workflow_yaml: str | None) -> tuple[Path, Path]:
        config_path = tmp_path / ".pre-commit-config.yaml"
        config_path.write_text(precommit_yaml, encoding="utf-8")
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        if workflow_yaml is not None:
            (workflows_dir / "ci.yml").write_text(workflow_yaml, encoding="utf-8")
        return config_path, workflows_dir

    def test_fires_on_blocking_hook_with_no_ci_equivalent(self, tmp_path):
        precommit = (
            "repos:\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: foo-blocking\n"
            "        name: foo (blocking)\n"
            "        entry: python -m foo\n"
            "        language: system\n"
        )
        workflow = "jobs:\n  test:\n    steps:\n      - name: run pytest\n        run: pytest\n"
        config_path, workflows_dir = self._write_tree(tmp_path, precommit, workflow)
        violations = _find_violations(config_path, workflows_dir)
        assert len(violations) == 1
        assert "foo-blocking" in violations[0]
        assert "NO matching CI step" in violations[0]

    def test_fires_when_only_matching_ci_step_is_continue_on_error(self, tmp_path):
        precommit = (
            "repos:\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: bar-blocking\n"
            "        name: bar (blocking)\n"
            "        entry: python -m bar\n"
            "        language: system\n"
        )
        workflow = "jobs:\n" "  lint:\n" "    steps:\n" "      - name: bar check\n" "        run: python -m bar\n" "        continue-on-error: true\n"
        config_path, workflows_dir = self._write_tree(tmp_path, precommit, workflow)
        violations = _find_violations(config_path, workflows_dir)
        assert len(violations) == 1
        assert "bar-blocking" in violations[0]
        assert "continue-on-error" in violations[0]

    def test_silent_when_blocking_ci_step_matches(self, tmp_path):
        precommit = (
            "repos:\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: baz-blocking\n"
            "        name: baz (blocking)\n"
            "        entry: python -m baz\n"
            "        language: system\n"
        )
        workflow = "jobs:\n  lint:\n    steps:\n      - name: baz check\n        run: python -m baz\n"
        config_path, workflows_dir = self._write_tree(tmp_path, precommit, workflow)
        violations = _find_violations(config_path, workflows_dir)
        assert violations == []

    def test_repo_comment_triggers_detection_without_id_suffix(self, tmp_path):
        precommit = (
            "repos:\n"
            "  # Custom gate. Blocking -- always enforced locally, no CI job mirrors it yet.\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: custom-tool\n"
            "        name: custom tool\n"
            "        entry: python -m custom_tool\n"
            "        language: system\n"
        )
        workflow = "jobs:\n  test:\n    steps:\n      - name: run pytest\n        run: pytest\n"
        config_path, workflows_dir = self._write_tree(tmp_path, precommit, workflow)
        violations = _find_violations(config_path, workflows_dir)
        assert len(violations) == 1
        assert "custom-tool" in violations[0]
        assert "repo-comment" in violations[0]

    def test_lowercase_blocking_word_in_comment_not_falsely_flagged(self, tmp_path):
        # Regression test for the exact false-positive shape found in this
        # repo's own lint-advisory-warn hook comment: lowercase "blocking"
        # used as an ordinary adjective describing a DIFFERENT hook, not a
        # self-declaration that THIS hook is blocking.
        precommit = (
            "repos:\n"
            "  # Advisory gate: surfaces everything the blocking gate's checks hide. Never blocks.\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: advisory-tool\n"
            "        name: advisory tool\n"
            "        entry: python -m advisory_tool\n"
            "        language: system\n"
        )
        config_path, workflows_dir = self._write_tree(tmp_path, precommit, None)
        violations = _find_violations(config_path, workflows_dir)
        assert violations == []

    def test_allcaps_blocking_word_in_comment_is_flagged(self, tmp_path):
        # Regression test for the false-negative shape found during review:
        # this repo's real mypy-blocking hook comment reads
        # "# MyPy: whole-project, BLOCKING." (all-caps, period-terminated,
        # no "the " prefix) -- the comment fallback must catch this
        # capitalization too, not just title-case "Blocking".
        precommit = (
            "repos:\n"
            "  # Custom security gate. BLOCKING -- enforced locally, no CI mirrors it.\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: custom-security-check\n"
            "        name: custom security check\n"
            "        entry: python -m custom_security_check\n"
            "        language: system\n"
        )
        workflow = "jobs:\n  test:\n    steps:\n      - name: run pytest\n        run: pytest\n"
        config_path, workflows_dir = self._write_tree(tmp_path, precommit, workflow)
        violations = _find_violations(config_path, workflows_dir)
        assert len(violations) == 1
        assert "custom-security-check" in violations[0]
        assert "repo-comment" in violations[0]

    def test_allowlisted_hook_never_flagged_even_if_id_matches_convention(self, tmp_path, monkeypatch):
        precommit = (
            "repos:\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: detect-secrets-blocking\n"
            "        name: pretend-allowlisted (blocking)\n"
            "        entry: python -m detect_secrets\n"
            "        language: system\n"
        )
        config_path, workflows_dir = self._write_tree(tmp_path, precommit, None)
        monkeypatch.setitem(_LOCAL_ONLY_ALLOWLIST, "detect-secrets-blocking", "test-only allowlist entry")
        violations = _find_violations(config_path, workflows_dir)
        assert violations == []
