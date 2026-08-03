"""Regression test for .github/workflows/release.yml's tag-vs-version guard steps.

Regression (2026-07-22 session, release-hardening pass): release.yml gained two shell guard
steps -- "Assert git tag matches package version" (runs on the ``release`` event; compares
``GITHUB_REF_NAME`` against pyproject.toml's ``[project].version``) and "Assert a matching
release tag exists (workflow_dispatch)" (runs on manual dispatch, which has no tag to compare
against; instead requires a ``v<version>`` tag to already EXIST in the repo). Neither step had
any test coverage validating the shell logic itself actually catches a real mismatch -- a typo
in either snippet would silently let a bad release through undetected until it broke a live
publish. Following pyutilz's tests/test_meta/test_publish_workflow_version_check.py pattern,
this test extracts each step's ``run:`` shell snippet straight from the workflow YAML and RUNS
it (not just greps for a string), so a future edit that breaks either comparison is caught by
a failing test rather than discovered mid-release.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

import pytest

yaml = pytest.importorskip("yaml")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "release.yml"
_RELEASE_STEP_NAME = "Assert git tag matches package version"
_DISPATCH_STEP_NAME = "Assert a matching release tag exists (workflow_dispatch)"


def _find_step(name: str) -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["build"]["steps"]:
        if step.get("name") == name:
            return dict(step)
    raise AssertionError(f"step {name!r} not found in {_WORKFLOW_PATH}")


def _current_project_version() -> str:
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version = "(.*)"$', text, re.MULTILINE)
    assert m is not None, 'pyproject.toml has no top-level version = "..." line'
    return m.group(1)


def _run_release_check(script: str, ref_name: str) -> subprocess.CompletedProcess:
    """Run the ``release``-event guard's shell snippet against the real repo root -- it only
    reads pyproject.toml and $GITHUB_REF_NAME, no git tag state involved."""
    env = dict(os.environ)
    env["GITHUB_REF_NAME"] = ref_name
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_dispatch_check(script: str, repo_dir: Path) -> subprocess.CompletedProcess:
    """Run the ``workflow_dispatch`` guard's shell snippet against an isolated throwaway git
    repo -- it shells out to ``git rev-parse --verify refs/tags/v$PKG``, so exercising both the
    tag-present and tag-absent paths needs real, controllable tag state (the real llm_bench repo
    currently carries no ``v<version>`` tags at all, so it can't stand in for the pass path)."""
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _make_repo(tmp_path: Path, version: str, tag: Optional[str]) -> Path:
    """Build a throwaway git repo (one commit) with pyproject.toml pinned to ``version``, and
    -- if ``tag`` is given -- a lightweight tag of that name pointing at the commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(f'[project]\nname = "dummy"\nversion = "{version}"\n', encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, timeout=30)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    if tag is not None:
        git("tag", tag)
    return repo


class TestReleaseTagVersionGuard:
    """"Assert git tag matches package version": ``${GITHUB_REF_NAME#v}`` must equal
    pyproject.toml's ``[project].version``."""

    def test_step_exists_and_references_the_right_inputs(self):
        step = _find_step(_RELEASE_STEP_NAME)
        assert "GITHUB_REF_NAME" in step["run"]
        assert "pyproject.toml" in step["run"]

    def test_matching_tag_and_version_passes(self):
        step = _find_step(_RELEASE_STEP_NAME)
        current = _current_project_version()
        result = _run_release_check(step["run"], ref_name=f"v{current}")
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    def test_mismatched_tag_and_version_fails(self):
        step = _find_step(_RELEASE_STEP_NAME)
        current = _current_project_version()
        mismatched = f"{current}-does-not-exist"
        result = _run_release_check(step["run"], ref_name=f"v{mismatched}")
        assert result.returncode != 0
        assert mismatched in result.stdout or mismatched in result.stderr

    def test_tag_missing_v_prefix_is_treated_as_the_version_itself(self):
        """``${GITHUB_REF_NAME#v}`` only strips a LEADING literal "v" -- a malformed ref
        without that prefix compares as-is. Not reachable in practice (the workflow only fires
        this step on a ``release`` event, which always carries a ``v*`` tag) -- documented here
        so a future reader of the shell doesn't mistake this for a bug."""
        step = _find_step(_RELEASE_STEP_NAME)
        current = _current_project_version()
        result = _run_release_check(step["run"], ref_name=current)
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


class TestDispatchTagExistsGuard:
    """"Assert a matching release tag exists (workflow_dispatch)": requires a
    ``v<pyproject-version>`` tag to already exist in the repo's tag history."""

    def test_step_exists_and_references_the_right_inputs(self):
        step = _find_step(_DISPATCH_STEP_NAME)
        assert "pyproject.toml" in step["run"]
        assert "refs/tags/v$PKG" in step["run"]

    def test_matching_release_tag_present_passes(self, tmp_path):
        step = _find_step(_DISPATCH_STEP_NAME)
        repo = _make_repo(tmp_path, version="9.9.9-test", tag="v9.9.9-test")
        result = _run_dispatch_check(step["run"], repo)
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    def test_missing_release_tag_fails(self, tmp_path):
        step = _find_step(_DISPATCH_STEP_NAME)
        repo = _make_repo(tmp_path, version="9.9.9-test", tag=None)
        result = _run_dispatch_check(step["run"], repo)
        assert result.returncode != 0
        assert "9.9.9-test" in result.stdout or "9.9.9-test" in result.stderr

    def test_tag_for_a_different_version_does_not_satisfy_the_guard(self, tmp_path):
        """A stale/unrelated tag sitting in the repo must not accidentally satisfy the check --
        only a tag matching the CURRENT pyproject.toml version counts."""
        step = _find_step(_DISPATCH_STEP_NAME)
        repo = _make_repo(tmp_path, version="9.9.9-test", tag="v1.0.0-other")
        result = _run_dispatch_check(step["run"], repo)
        assert result.returncode != 0
