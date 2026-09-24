"""Test files are named after what they cover, not after the audit wave, round or batch that produced them.

``py_ci_shared.audit_wave_filenames`` holds the shared stem patterns (``test_wave97_``, ``test_round17_``,
``test_audit_2026_`` ...). This repo adds three of its own the shared list lacks: ``test_rounds<n>_``,
``test_batch<n>_`` and ``test_phase<n>_``. Docstrings citing a finding stay legal: only file names are checked.
"""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.audit_wave_filenames import assert_no_new_audit_wave_filenames, find_audit_wave_test_files

TESTS_DIR = Path(__file__).resolve().parents[1]
_EXTRA_PATTERNS = (r"^test_rounds\d+_", r"^test_batch\d+_", r"^test_phase\d+_")


def test_no_audit_wave_filenames() -> None:
    assert_no_new_audit_wave_filenames(TESTS_DIR, extra_patterns=_EXTRA_PATTERNS)


def test_repo_specific_patterns_fire(tmp_path):
    """The three local patterns reach the shared scanner: each stem is reported, a topic name is not."""
    for stem in ("test_rounds2_x", "test_batch3_x", "test_phase4_x", "test_reporting_split"):
        (tmp_path / f"{stem}.py").write_text("def test_x(): pass\n", encoding="utf-8")
    found = {Path(p).stem for p in find_audit_wave_test_files(tmp_path, extra_patterns=_EXTRA_PATTERNS)}
    assert found == {"test_rounds2_x", "test_batch3_x", "test_phase4_x"}
