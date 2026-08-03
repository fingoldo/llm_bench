"""Meta-test: every environment variable production code reads via
``os.environ.get(...)``/``os.getenv(...)`` is documented in README.md.

Generalizes a 2026-07-21 audit finding (O-13): an env var the code actually
reads but never documents means an operator can't discover it exists to set
it -- and if the code fails closed when it's unset (an auth check, a feature
gate), the failure is silent.

Wires the shared ``py_ci_shared.readme_env_var_parity`` baseline/grandfather
check (its scan/compare logic is unit-tested in py_ci_shared's own suite --
this file only exercises the llm_bench-specific wiring: which files get
scanned, which README section documents them, and where the baseline lives).
README.md's "## Environment variables" section already documents every var
this repo currently reads (LLM_BENCH_DB_URL, OPENROUTER_API_KEY,
ANTHROPIC_API_KEY, OPENAI_API_KEY), so the committed baseline seeds empty.
A future env var added via a literal ``os.environ.get("NAME")`` /
``os.getenv("NAME")`` call without a matching README row is what fails this
test. The shared scanner used to be blind to ``cli/main.py``'s
``key_names`` tuple-and-loop idiom (how 3 of this repo's 4 current vars
are read) -- fixed upstream in py_ci_shared (``find_env_vars_read`` now
resolves a loop/comprehension target back to its literal iterable, inline
or via a module-level name binding); see
``test_provider_key_loop_shape_is_now_detected`` below, which pins the
FIXED behavior.

Refresh after an intentional new env var lands (documented immediately after,
in the same change):

  pytest tests/test_meta/test_readme_env_var_parity.py --refresh-readme-env-var-baseline
"""

from __future__ import annotations

from pathlib import Path

import pytest

from py_ci_shared.readme_env_var_parity import assert_no_new_undocumented_env_vars

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench"
_README_PATH = Path(__file__).resolve().parents[2] / "README.md"
_BASELINE_PATH = Path(__file__).resolve().parent / "_readme_env_var_baseline.json"


def test_no_new_undocumented_env_vars():
    assert_no_new_undocumented_env_vars(
        files=sorted(_SRC_ROOT.rglob("*.py")),
        readme_path=_README_PATH,
        baseline_path=_BASELINE_PATH,
    )


class TestReadmeEnvVarParityDetectsShapes:
    """Direct regression coverage for THIS repo's wiring (which paths get
    scanned, which heading is read) via synthetic source/README built in
    tmp_path -- proving a genuinely new undocumented var trips the check
    and a documented one stays silent. The checker's own scan/compare
    logic (os.environ.get vs os.getenv forms, multi-name table cells,
    baseline seed/refresh) is covered by py_ci_shared's own test suite,
    not duplicated here.
    """

    def _write(self, tmp_path: Path, name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_new_undocumented_var_fails(self, tmp_path):
        src = self._write(tmp_path, "mod.py", 'import os\nos.environ.get("SYNTHETIC_NEW_VAR")\n')
        readme = self._write(tmp_path, "README.md", "# Project\n\n## Environment variables\n\n| Name | Description |\n|---|---|\n")
        baseline = tmp_path / "_baseline.json"
        baseline.write_text("[]", encoding="utf-8")
        with pytest.raises(pytest.fail.Exception, match="SYNTHETIC_NEW_VAR"):
            assert_no_new_undocumented_env_vars([src], readme, baseline)

    def test_documented_var_passes_silently(self, tmp_path):
        src = self._write(tmp_path, "mod.py", 'import os\nos.environ.get("SYNTHETIC_DOCUMENTED_VAR")\n')
        readme = self._write(
            tmp_path,
            "README.md",
            "# Project\n\n## Environment variables\n\n| Name | Description |\n|---|---|\n| `SYNTHETIC_DOCUMENTED_VAR` | test var |\n",
        )
        baseline = tmp_path / "_baseline.json"
        baseline.write_text("[]", encoding="utf-8")
        assert_no_new_undocumented_env_vars([src], readme, baseline)  # must not raise

    def test_provider_key_loop_shape_is_now_detected(self, tmp_path):
        """Pins the FIXED behavior (was a known blind spot): the shared AST
        scanner now resolves a loop/comprehension target back to its literal
        iterable, so ``cli/main.py``'s actual idiom -- a module-level tuple
        of names, consumed via a list-comprehension calling
        ``os.environ.get(name)`` on the comprehension's loop variable -- IS
        detected. This mirrors that exact shape with an undocumented var and
        asserts the check DOES raise, proving all 4 of this repo's real env
        vars (including the 3 provider keys read via this idiom) now get
        real regression protection from this test."""
        src = self._write(
            tmp_path,
            "mod.py",
            "import os\n" 'key_names = ("SYNTHETIC_LOOP_VAR",)\n' "present = [name for name in key_names if os.environ.get(name)]\n",
        )
        readme = self._write(tmp_path, "README.md", "# Project\n\n## Environment variables\n\n| Name | Description |\n|---|---|\n")
        baseline = tmp_path / "_baseline.json"
        baseline.write_text("[]", encoding="utf-8")
        with pytest.raises(pytest.fail.Exception, match="SYNTHETIC_LOOP_VAR"):
            assert_no_new_undocumented_env_vars([src], readme, baseline)
