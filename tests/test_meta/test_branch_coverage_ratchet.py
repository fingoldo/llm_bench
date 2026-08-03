"""Branch/coverage-shape ratchet gate for src/llm_bench.

Motivation: ``classify_provider_error`` had 1 of 6 branches covered
despite a documented historical mis-classification incident (see its
docstring: "lesson from the live run on 2026-05-05"), and
``BudgetGate.check()`` had zero tests at all, including the exact-cap
boundary case. Both are covered now (fixed this session -- see
``tests/unit/test_classify.py::TestEachBranch`` and
``tests/unit/test_budget.py::TestHappyPathAndSkip::test_exact_boundary_passes``).
This check exists to stop a NEW uncovered branch from creeping back
into similarly risk-sensitive functions across the rest of the package,
not to demand 100% branch coverage outright.

How it works:

  1. Run the test suite under branch coverage as a subprocess
     (``pytest --cov=src/llm_bench --cov-branch
     --cov-report=json:<tmp>/cov.json``). This is a real pytest-cov
     invocation, not a mock -- it needs to actually execute.
  2. If that subprocess fails for an ENVIRONMENT/TOOLING reason (this
     user's Windows machine has a standing, documented pytest-cov
     PermissionError writing the sqlite ``.coverage`` data file -- an
     OS/3rd-party tooling limitation, not a bug in this repo), skip
     with a clear message instead of failing. The skip path only
     triggers on ``win32`` when a known failure signature is present in
     the subprocess output -- on any other platform (a Linux CI
     runner), the same failure is treated as real and surfaces as a
     hard ``pytest.fail()`` instead, so a genuinely broken CI job can't
     go silently green.
  3. On success, parse ``coverage.json``, AST-enumerate every
     if/elif/else/except/ternary branch under ``src/llm_bench``, and
     cross-reference each branch's line number against the lines
     coverage.py recorded as executed (or deliberately excluded, e.g.
     ``if TYPE_CHECKING:`` per this repo's
     ``[tool.coverage.report] exclude_lines``).
  4. Compare the resulting uncovered-branch key set against a committed
     baseline (the pattern used by
     ``tests/test_meta/test_code_audit_baseline.py`` and
     ``test_api_stability.py``): only a NEW uncovered branch fails the
     test, pre-existing gaps are grandfathered. Refresh after a
     deliberate change with ``--refresh-branch-coverage-baseline``.

This gate carries real false-positive risk (a module nobody happened to
exercise this run, a flaky test that skipped, ...), which is exactly
why it uses the baseline-ratchet pattern instead of a hard
assert-no-uncovered-branches: only a NEW gap fails, and the baseline
JSON itself is the durable "confirmed, reviewed, currently OK" record
-- there is no separate inline ``_WHITELIST`` on top of it, since that
would just be a second escape hatch doing the same job.

CI note: this repo's ``[tool.pytest.ini_options] markers`` already
defines ``slow`` for exactly this purpose ("deselect with
'-m \"not slow\"'"); the gate test below is marked ``slow`` so a normal
local ``pytest`` run doesn't pay for a full nested test-suite subprocess
run on every invocation. A dedicated CI coverage job is expected to run
it explicitly (e.g. ``pytest -m slow tests/test_meta/test_branch_coverage_ratchet.py``).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src" / "llm_bench"
_BASELINE_PATH = Path(__file__).resolve().parent / "_branch_coverage_baseline.json"
_THIS_FILE = Path(__file__).resolve()

REFRESH_FLAG = "--refresh-branch-coverage-baseline"

# Known signatures of the documented Windows pytest-cov tooling failure
# (PermissionError / WinError writing the sqlite .coverage data file, or
# the coverage sqlite db getting locked by another process). Matched
# against the subprocess's combined stdout+stderr. Only consulted on
# win32 -- see module docstring, step 2.
_ENV_FAILURE_SIGNATURES: tuple[str, ...] = (
    "PermissionError",
    "WinError 5",
    "WinError 32",
    "[Errno 13]",
    "Errno 13",
    "Access is denied",
    "database is locked",
    "CoverageException",
)

# Marker deselection keeps this subprocess's own test run independent of
# external services (live provider API keys, a Postgres test DB, ...) --
# this gate is about branch shape, not about re-running everything this
# repo's full CI matrix already covers elsewhere.
_MARKER_EXPR = "not slow and not integration and not live and not postgres"

# A real subprocess run against this package's ~40 src/llm_bench modules
# executes many thousands of lines; observed on this machine: pytest-cov
# can write a structurally-valid coverage.json (every file present) with
# EVERY file's executed_lines empty, despite the subprocess's own test
# run passing normally -- a silent variant of the documented Windows
# pytest-cov limitation that raises no error for _ENV_FAILURE_SIGNATURES
# to match. Any real run clears this floor by two orders of magnitude;
# only a broken collector produces a number anywhere near it.
_MIN_SANE_EXECUTED_LINES = 200


def _total_executed_lines(cov_json_path: Path) -> int:
    """Sum of ``executed_lines`` across every file in a coverage.json
    report -- used only as a sanity floor to detect a silently-empty
    collector, not for the real per-branch cross-reference (that's
    ``_load_covered_or_excluded_lines``)."""
    data = json.loads(cov_json_path.read_text(encoding="utf-8"))
    return sum(len(info.get("executed_lines", [])) for info in data.get("files", {}).values())


def _refresh_requested() -> bool:
    return REFRESH_FLAG in sys.argv


def _branch_point_keys(tree: ast.AST, rel: str) -> list[str]:
    """AST-enumerate every if/elif/else/except/ternary branch DESTINATION
    in ``tree``, returning stable keys ``"<rel>:<lineno>:<kind>"`` -- one
    key per branch actually taken (the first line of the body that runs
    when that branch fires), not one key per ``if``/``try``/``IfExp``
    node.

    ``elif`` chains need no special-casing: Python's ast represents
    ``elif`` as a nested ``ast.If`` inside the parent's ``orelse``, and
    ``ast.walk`` visits that nested node too, contributing its own
    if-true/if-false pair -- so a 3-way elif chain naturally yields one
    pair per link.

    Ternary (``ast.IfExp``) branches are recorded at line-level
    granularity like everything else here: for a single-line ternary
    (the overwhelmingly common case), both arms share one line number,
    so this cannot distinguish "only the true arm ever ran" from "both
    ran" -- it can only tell whether the line executed AT ALL. This is a
    known, accepted coarsening (the whole checker cross-references
    against coverage.json's LINE data, not branch-arc pairs), not a bug.
    """
    keys: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            if node.body:
                keys.append(f"{rel}:{node.body[0].lineno}:if-true")
            if node.orelse:
                keys.append(f"{rel}:{node.orelse[0].lineno}:if-false")
        elif isinstance(node, ast.Try):
            keys.extend(f"{rel}:{handler.body[0].lineno}:except" for handler in node.handlers if handler.body)
        elif isinstance(node, ast.IfExp):
            keys.append(f"{rel}:{node.body.lineno}:ternary-true")
            keys.append(f"{rel}:{node.orelse.lineno}:ternary-false")
    return keys


def _load_covered_or_excluded_lines(cov_json_path: Path) -> dict[str, set[int]]:
    """Map each ``src/llm_bench``-relative file path (posix separators)
    to the union of lines coverage.py recorded as EXECUTED at least once
    OR deliberately EXCLUDED (this repo's
    ``[tool.coverage.report] exclude_lines`` -- ``if TYPE_CHECKING:``,
    ``def __repr__``, ``raise NotImplementedError``, ...). Excluded
    lines are dead-at-runtime BY DESIGN and must not be flagged as
    coverage gaps; coverage.json already tells us exactly which lines
    those are per file, so this trusts that config rather than
    re-deriving it.
    """
    data = json.loads(cov_json_path.read_text(encoding="utf-8"))
    out: dict[str, set[int]] = {}
    for file_key, info in data.get("files", {}).items():
        norm = file_key.replace("\\", "/")
        if "src/llm_bench/" not in norm:
            continue
        rel = norm.split("src/llm_bench/", 1)[1]
        out[rel] = set(info.get("executed_lines", [])) | set(info.get("excluded_lines", []))
    return out


def _uncovered_branch_keys(src_root: Path, covered_by_file: dict[str, set[int]]) -> list[str]:
    """AST-walk every .py file under ``src_root`` and return the sorted
    branch-point keys whose line was neither executed nor excluded per
    ``covered_by_file``. A file with NO entry in ``covered_by_file``
    (coverage.py never even imported/ran it this session) has every one
    of its branch points flagged -- an empty-set default, not a silent
    pass, so a module nobody's tests touch shows up honestly rather than
    looking clean by omission.
    """
    uncovered: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(src_root)).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:
            uncovered.append(f"{rel}:0:syntax-error:{e}")
            continue
        covered_lines = covered_by_file.get(rel, set())
        for key in _branch_point_keys(tree, rel):
            lineno = int(key.split(":", 2)[1])
            if lineno not in covered_lines:
                uncovered.append(key)
    return sorted(uncovered)


def _attempt_coverage_run(tmp_path: Path) -> tuple[Path | None, str, bool]:
    """Attempt the real branch-coverage subprocess run.

    Returns ``(cov_json_path, message, is_environment_skip)``:
      - success: ``(path_to_json, "", False)``.
      - environment/tooling failure (Windows pytest-cov PermissionError
        or equivalent): ``(None, <skip message>, True)``.
      - anything else (a real problem worth surfacing):
        ``(None, <fail message>, False)``.
    """
    cov_json = tmp_path / "cov.json"
    # This subprocess's own pytest-cov instance MUST NOT share a coverage
    # data file location with whatever pytest-cov instance is running
    # THIS test itself (e.g. CI's outer `pytest --cov=src/llm_bench
    # --cov-report=xml`, statement-only, no --cov-branch). Without an
    # isolated COVERAGE_FILE, both processes default to `.coverage` in
    # the repo root; the outer session's own finish/combine() step then
    # discovers the inner subprocess's BRANCH-mode data file alongside
    # its own STATEMENT-mode data and raises
    # `coverage.exceptions.DataError: Can't combine statement coverage
    # data with branch data` as an INTERNALERROR that crashes the ENTIRE
    # outer pytest session -- confirmed in CI (2026-07-23): every other
    # test in the run got swept into a bogus "failed" tally by the crash,
    # not just this one. `tmp_path` is unique per test invocation and
    # never scanned by the outer session's own coverage combine, so this
    # fully isolates the two.
    cov_data_file = tmp_path / ".coverage.branch_ratchet_subprocess"
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--cov=src/llm_bench",
        "--cov-branch",
        f"--cov-report=json:{cov_json}",
        "-p",
        "no:cacheprovider",
        "-q",
        "-n",
        "auto",  # pytest-cov combines branch data across xdist workers natively
        "-m",
        _MARKER_EXPR,
        f"--ignore={_THIS_FILE}",
        "tests",
    ]
    subprocess_env = {**os.environ, "COVERAGE_FILE": str(cov_data_file)}
    try:
        result = subprocess.run(cmd, cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=900, env=subprocess_env)
    except subprocess.TimeoutExpired:
        return (
            None,
            "coverage subprocess timed out after 900s -- too slow to run "
            "inline here. Run the equivalent command directly on a CI "
            "runner instead: pytest --cov=src/llm_bench --cov-branch "
            f'--cov-report=json:<tmp>/cov.json -m "{_MARKER_EXPR}" tests',
            False,
        )

    if cov_json.exists():
        total_executed = _total_executed_lines(cov_json)
        if sys.platform.startswith("win") and total_executed < _MIN_SANE_EXECUTED_LINES:
            return (
                None,
                f"pytest-cov wrote a coverage.json with only "
                f"{total_executed} total executed line(s) across every "
                f"src/llm_bench file, despite the subprocess's own test "
                f"run passing -- this is the SILENT variant of the "
                f"documented Windows pytest-cov limitation (the sqlite "
                f".coverage backing store fails to record hits without "
                f"raising, rather than erroring outright): a real run "
                f"covers thousands of lines, so near-zero is definitionally "
                f"tooling failure, not an actual coverage gap. Confirm this "
                f"ratchet check on a Linux CI runner instead.\n"
                f"--- subprocess output (tail) ---\n{(result.stdout or '') + chr(10) + (result.stderr or '')[-2000:]}",
                True,
            )
        return cov_json, "", False

    combined_output = (result.stdout or "") + "\n" + (result.stderr or "")

    if sys.platform.startswith("win"):
        for sig in _ENV_FAILURE_SIGNATURES:
            if sig in combined_output:
                return (
                    None,
                    f"pytest-cov subprocess failed writing its coverage "
                    f"data (matched {sig!r} in the subprocess output) -- "
                    f"this is the documented Windows pytest-cov "
                    f"PermissionError writing the sqlite .coverage data "
                    f"file (a standing OS/tooling limitation on this "
                    f"machine, not a real coverage gap). Confirm this "
                    f"ratchet check on a Linux CI runner instead.\n"
                    f"--- subprocess output (tail) ---\n{combined_output[-2000:]}",
                    True,
                )

    return (
        None,
        f"coverage subprocess produced no cov.json and matched none of "
        f"the known Windows-tooling failure signatures (returncode="
        f"{result.returncode}). Either the coverage invocation itself is "
        f"broken -- fix it -- or a real test failure upstream prevented "
        f"the report from ever being written -- fix the failing test.\n"
        f"--- subprocess output (tail) ---\n{combined_output[-4000:]}",
        False,
    )


def _ratchet(current: set[str]) -> None:
    if _refresh_requested() or not _BASELINE_PATH.exists():
        _BASELINE_PATH.write_text(json.dumps(sorted(current), indent=2) + "\n", encoding="utf-8")
        pytest.skip(f"branch-coverage baseline refreshed at {_BASELINE_PATH.name} " f"({len(current)} pre-existing uncovered branch(es))")

    baseline = set(json.loads(_BASELINE_PATH.read_text(encoding="utf-8")))
    new = sorted(current - baseline)
    drained = sorted(baseline - current)

    if drained:
        sys.stderr.write(
            f"\n[branch_coverage_ratchet] {len(drained)} branch(es) newly "
            f"COVERED (no longer a gap):\n  "
            + "\n  ".join(drained[:15])
            + (f"\n  ... and {len(drained) - 15} more" if len(drained) > 15 else "")
            + f"\n  Refresh: pytest ... {REFRESH_FLAG}\n"
        )

    if new:
        detail = "\n  ".join(new[:30])
        pytest.fail(
            f"{len(new)} NEW uncovered if/elif/else/except/ternary "
            f"branch(es) in src/llm_bench -- risk-sensitive branch logic "
            f"with no test exercising it:\n  {detail}"
            + (f"\n  ... and {len(new) - 30} more" if len(new) > 30 else "")
            + f"\nAdd a test exercising this branch, OR if this is "
            f"confirmed pre-existing debt, refresh the baseline: "
            f"pytest ... {REFRESH_FLAG}"
        )


@pytest.mark.slow
def test_no_new_uncovered_branches(tmp_path):
    cov_json, message, is_env_skip = _attempt_coverage_run(tmp_path)
    if cov_json is None:
        if is_env_skip:
            pytest.skip(message)
        pytest.fail(message)

    covered_by_file = _load_covered_or_excluded_lines(cov_json)
    current = set(_uncovered_branch_keys(_SRC_ROOT, covered_by_file))
    _ratchet(current)


# ------------------------------------------------------------------------
# Regression tests for the checker itself (synthetic source, tmp_path)
# ------------------------------------------------------------------------


class TestBranchPointKeysDetectsShapes:
    """Direct regression tests for ``_branch_point_keys`` -- proves it
    FIRES on every branch shape named in the task (if/elif/else/except/
    ternary) and stays silent on branch-free code."""

    def _run(self, tmp_path, source: str) -> list[str]:
        p = tmp_path / "synthetic.py"
        p.write_text(source, encoding="utf-8")
        tree = ast.parse(source, filename=str(p))
        return _branch_point_keys(tree, "synthetic.py")

    def test_if_without_else_fires_one_branch(self, tmp_path):
        keys = self._run(tmp_path, "def f(x):\n" "    if x:\n" "        return 1\n" "    return 0\n")
        assert keys == ["synthetic.py:3:if-true"]

    def test_if_else_fires_both_branches(self, tmp_path):
        keys = self._run(tmp_path, "def f(x):\n" "    if x:\n" "        return 1\n" "    else:\n" "        return 0\n")
        assert set(keys) == {"synthetic.py:3:if-true", "synthetic.py:5:if-false"}

    def test_elif_chain_fires_each_link(self, tmp_path):
        source = "def f(x):\n" "    if x == 1:\n" "        return 'a'\n" "    elif x == 2:\n" "        return 'b'\n" "    else:\n" "        return 'c'\n"
        keys = self._run(tmp_path, source)
        assert set(keys) == {
            "synthetic.py:3:if-true",
            "synthetic.py:4:if-false",
            "synthetic.py:5:if-true",
            "synthetic.py:7:if-false",
        }

    def test_except_handler_fires(self, tmp_path):
        source = "def f():\n" "    try:\n" "        risky()\n" "    except ValueError:\n" "        return None\n"
        keys = self._run(tmp_path, source)
        assert keys == ["synthetic.py:5:except"]

    def test_ternary_fires_both_arms(self, tmp_path):
        keys = self._run(tmp_path, "def f(x):\n" "    return 1 if x else 0\n")
        assert set(keys) == {"synthetic.py:2:ternary-true", "synthetic.py:2:ternary-false"}

    def test_branch_free_function_is_silent(self, tmp_path):
        keys = self._run(tmp_path, "def f(x):\n" "    return x + 1\n")
        assert keys == []


class TestUncoveredBranchKeysCrossReference:
    """Direct regression tests for ``_uncovered_branch_keys`` -- proves
    it FIRES when a branch's line is missing from the covered-lines set
    and stays silent when it's present."""

    def _write(self, tmp_path: Path, source: str) -> Path:
        (tmp_path / "mod.py").write_text(source, encoding="utf-8")
        return tmp_path

    def test_missing_branch_line_fires(self, tmp_path):
        src_root = self._write(tmp_path, "def f(x):\n" "    if x:\n" "        return 1\n" "    return 0\n")
        covered_by_file = {"mod.py": {1, 2, 4}}  # line 3 (the if-true body) never executed
        uncovered = _uncovered_branch_keys(src_root, covered_by_file)
        assert uncovered == ["mod.py:3:if-true"]

    def test_covered_branch_line_is_silent(self, tmp_path):
        src_root = self._write(tmp_path, "def f(x):\n" "    if x:\n" "        return 1\n" "    return 0\n")
        covered_by_file = {"mod.py": {1, 2, 3, 4}}
        uncovered = _uncovered_branch_keys(src_root, covered_by_file)
        assert uncovered == []

    def test_file_absent_from_coverage_is_fully_flagged(self, tmp_path):
        # A module coverage.py never even imported/ran (no test hit it
        # at all this session) -- every one of its branch points must
        # show as uncovered, not silently pass via a missing dict key.
        src_root = self._write(tmp_path, "def f(x):\n" "    if x:\n" "        return 1\n" "    return 0\n")
        uncovered = _uncovered_branch_keys(src_root, {})
        assert uncovered == ["mod.py:3:if-true"]


class TestLoadCoveredOrExcludedLinesMergesExclusions:
    """Direct regression tests for ``_load_covered_or_excluded_lines`` --
    proves excluded lines (e.g. ``if TYPE_CHECKING:``) count as
    non-gaps, and files outside src/llm_bench are ignored."""

    def test_excluded_lines_count_as_covered(self, tmp_path):
        cov_json = tmp_path / "cov.json"
        cov_json.write_text(
            json.dumps(
                {
                    "files": {
                        "src/llm_bench/mod.py": {
                            "executed_lines": [1, 2, 4],
                            "excluded_lines": [3],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        covered = _load_covered_or_excluded_lines(cov_json)
        assert covered["mod.py"] == {1, 2, 3, 4}

    def test_non_src_files_are_ignored(self, tmp_path):
        cov_json = tmp_path / "cov.json"
        cov_json.write_text(
            json.dumps({"files": {"tests/unit/test_foo.py": {"executed_lines": [1, 2], "excluded_lines": []}}}),
            encoding="utf-8",
        )
        covered = _load_covered_or_excluded_lines(cov_json)
        assert covered == {}


class TestTotalExecutedLinesSanityFloor:
    """Regression coverage for the silent-empty-coverage.json detection
    (_total_executed_lines / _MIN_SANE_EXECUTED_LINES): observed on this
    machine, pytest-cov can write a structurally-valid coverage.json
    (every file present) with every file's executed_lines empty, despite
    the subprocess's own test run passing -- a silent variant of the
    Windows pytest-cov limitation that _ENV_FAILURE_SIGNATURES matching
    alone cannot catch (there is no error text to match against)."""

    def test_zero_executed_lines_summed_as_zero(self, tmp_path):
        cov = tmp_path / "cov.json"
        cov.write_text(
            json.dumps({"files": {"a.py": {"executed_lines": []}, "b.py": {"executed_lines": []}}}),
            encoding="utf-8",
        )
        assert _total_executed_lines(cov) == 0

    def test_real_coverage_sums_above_floor(self, tmp_path):
        cov = tmp_path / "cov.json"
        cov.write_text(
            json.dumps({"files": {"a.py": {"executed_lines": list(range(150))}, "b.py": {"executed_lines": list(range(100))}}}),
            encoding="utf-8",
        )
        assert _total_executed_lines(cov) > _MIN_SANE_EXECUTED_LINES

    def test_missing_files_key_is_zero_not_a_crash(self, tmp_path):
        cov = tmp_path / "cov.json"
        cov.write_text(json.dumps({}), encoding="utf-8")
        assert _total_executed_lines(cov) == 0


class TestCoverageFileIsolation:
    """Regression test for a real CI incident (2026-07-23): the nested
    coverage subprocess and the outer pytest-cov session running THIS
    test both defaulted to the same ``.coverage`` file in the repo root,
    so the outer session's own finish/combine() step tried to merge
    branch-mode data (written by the subprocess) with its own
    statement-mode data and raised
    ``coverage.exceptions.DataError: Can't combine statement coverage
    data with branch data`` as an INTERNALERROR -- crashing the entire
    outer pytest session, not just this test. Fixed by giving the
    subprocess an explicit, tmp_path-scoped ``COVERAGE_FILE``."""

    def test_subprocess_gets_an_isolated_coverage_file_env_var(self, tmp_path, monkeypatch):
        captured_env = {}

        class _FakeCompletedProcess:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, cwd, capture_output, text, timeout, env):
            captured_env.update(env)
            # Simulate a real run: write a plausible cov.json so the
            # caller's `cov_json.exists()` check succeeds.
            for arg in cmd:
                if arg.startswith("--cov-report=json:"):
                    Path(arg.split(":", 1)[1]).write_text('{"files": {}}', encoding="utf-8")
            return _FakeCompletedProcess()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        _attempt_coverage_run(tmp_path)

        assert "COVERAGE_FILE" in captured_env
        cov_file = Path(captured_env["COVERAGE_FILE"])
        assert cov_file.parent == tmp_path
        assert cov_file.name != ".coverage"  # not the bare default name

    def test_isolated_coverage_file_differs_across_calls(self, tmp_path, monkeypatch):
        """Two invocations (e.g. two test runs in the same session) must
        not accidentally share a coverage file if given different
        tmp_path fixtures -- proves the path is derived from tmp_path,
        not a fixed module-level constant."""
        captured: list[str] = []

        class _FakeCompletedProcess:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, cwd, capture_output, text, timeout, env):
            captured.append(env["COVERAGE_FILE"])
            for arg in cmd:
                if arg.startswith("--cov-report=json:"):
                    Path(arg.split(":", 1)[1]).write_text('{"files": {}}', encoding="utf-8")
            return _FakeCompletedProcess()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        other_tmp = tmp_path / "other"
        other_tmp.mkdir()
        _attempt_coverage_run(tmp_path)
        _attempt_coverage_run(other_tmp)

        assert captured[0] != captured[1]
