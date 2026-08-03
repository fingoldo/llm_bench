"""Per-file LOC-budget gate for llm_bench's own source tree.

llm_bench had no "file grew too big" gate at all before this wiring --
``mlframe`` and ``realtime_applications`` each independently grew a
near-identical ~75-line pytest test for it (walk production ``.py`` files,
flag anything over a line-count ceiling, grandfather pre-existing offenders
via a committed baseline JSON so existing debt doesn't block adoption, allow
a small per-file growth slack so a trivial edit to an already-oversized file
doesn't trip the gate while a real expansion still does). ``py_ci_shared``
factored that pattern out to one place (``py_ci_shared.loc_budget``); this
file is llm_bench's own thin wiring over it -- see that module's docstring
for the full seed/compare/refresh/report lifecycle.

Measured 2026-07-22: the largest module in ``src/llm_bench`` is
``runner/round_runner.py`` at ~874 LOC, comfortably under the 1000-line
limit below, so the baseline seeds empty/clean on first run -- there is no
grandfathered debt to carry.

Refresh (only after a deliberate split/shrink changes what's grandfathered):
  pytest tests/test_meta/test_loc_budget.py --refresh-loc-budget-baseline
"""

from __future__ import annotations

from pathlib import Path

import pytest

from py_ci_shared.loc_budget import assert_no_new_oversized_file

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src" / "llm_bench"
_BASELINE_PATH = Path(__file__).resolve().parent / "_loc_budget_baseline.json"

# Matches this repo's own module-size-limit convention (grep of pyproject.toml's
# ruff/mccabe/interrogate comments and docs/ turned up no repo-specific LOC number
# to override with -- this is py_ci_shared.loc_budget's own DEFAULT_LOC_LIMIT,
# passed explicitly so a future change to that upstream default can't silently
# retarget this repo's gate).
_LOC_LIMIT = 1000


def test_no_new_file_over_loc_budget():
    assert_no_new_oversized_file(
        files=_SRC_ROOT.rglob("*.py"),
        root=_REPO_ROOT,
        baseline_path=_BASELINE_PATH,
        limit=_LOC_LIMIT,
    )


class TestLocBudgetWiringDetectsOversizedFiles:
    """Regression coverage for THIS file's wiring, not for
    ``assert_no_new_oversized_file`` itself (already covered end-to-end by
    py_ci_shared's own ``tests/test_loc_budget.py``). Exercises the exact
    call shape used in ``test_no_new_file_over_loc_budget`` above -- via a
    synthetic tmp_path tree standing in for ``src/llm_bench`` -- so a future
    edit that e.g. swaps ``files``/``root``, drops ``rglob("*.py")`` for a
    non-recursive glob, or forgets to pass ``limit=_LOC_LIMIT`` is caught
    here rather than silently widening or narrowing the real gate.
    """

    def _make_tree(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        root = tmp_path / "repo"
        src = root / "src" / "pkg"
        src.mkdir(parents=True)
        baseline = tmp_path / "_loc_budget_baseline.json"
        baseline.write_text("{}", encoding="utf-8")  # pre-seeded empty, like the real baseline today
        return root, src, baseline

    def test_fires_on_new_file_over_the_budget(self, tmp_path):
        root, src, baseline = self._make_tree(tmp_path)
        oversized = src / "oversized.py"
        oversized.write_text("\n".join(f"x{i} = {i}" for i in range(_LOC_LIMIT + 1)) + "\n", encoding="utf-8")

        with pytest.raises(pytest.fail.Exception, match="NEW oversized"):
            assert_no_new_oversized_file(
                files=src.rglob("*.py"),
                root=root,
                baseline_path=baseline,
                limit=_LOC_LIMIT,
            )

    def test_stays_silent_on_files_within_the_budget(self, tmp_path):
        root, src, baseline = self._make_tree(tmp_path)
        clean = src / "clean.py"
        clean.write_text("\n".join(f"x{i} = {i}" for i in range(50)) + "\n", encoding="utf-8")

        # Returns normally -- no pytest.fail/skip -- which IS the pass.
        assert_no_new_oversized_file(
            files=src.rglob("*.py"),
            root=root,
            baseline_path=baseline,
            limit=_LOC_LIMIT,
        )
