"""Meta-linter: forbid audit-wave/batch-ID filenames under tests/.

Filenames like ``test_wave97_*.py``, ``test_round17_*.py``, or
``test_batch3_*.py`` carry process metadata (which review pass, which
sprint) that belongs in git history or a PR description, not on disk --
a test's name should describe what it covers, not when it was written.
This project's own convention is different: docstrings freely cite a
specific audit finding for context (e.g. ``(audit: 04-High)``), which is
fine and must NOT be flagged here -- only a NUMBERED wave/round/batch
tag used as part of the organizing FILENAME is the actual anti-pattern.
"""

from __future__ import annotations

import re
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]

# Any test_*.py whose stem matches one of these patterns is rejected.
_FORBIDDEN_PATTERNS = [
    re.compile(r"^test_wave\d+_"),
    re.compile(r"^test_waves\d+_"),
    re.compile(r"^test_round\d+_"),
    re.compile(r"^test_rounds\d+_"),
    re.compile(r"^test_batch\d+_"),
    re.compile(r"^test_audit_\d{4}_"),
    re.compile(r"^test_phase\d+_"),
]


def _iter_test_files() -> list[Path]:
    return [p for p in _TESTS_ROOT.rglob("test_*.py") if "__pycache__" not in p.parts and p.name != Path(__file__).name]


def test_no_audit_wave_filenames() -> None:
    offenders: list[str] = []
    for path in _iter_test_files():
        stem = path.stem
        for pat in _FORBIDDEN_PATTERNS:
            if pat.match(stem):
                offenders.append(str(path.relative_to(_TESTS_ROOT)))
                break
    assert not offenders, f"Audit-wave/batch filenames must be renamed to topic-canonical names. Offenders: {offenders}"


class TestNoAuditWaveFilenamesDetectsShapes:
    def test_wave_prefix_detected(self, tmp_path):
        (tmp_path / "test_wave97_reporting_split.py").write_text("def test_x(): pass\n", encoding="utf-8")
        offenders = [str(p) for p in tmp_path.rglob("test_*.py") if any(pat.match(p.stem) for pat in _FORBIDDEN_PATTERNS)]
        assert offenders

    def test_round_prefix_detected(self, tmp_path):
        (tmp_path / "test_round17_valonly_null_detection.py").write_text("def test_x(): pass\n", encoding="utf-8")
        offenders = [str(p) for p in tmp_path.rglob("test_*.py") if any(pat.match(p.stem) for pat in _FORBIDDEN_PATTERNS)]
        assert offenders

    def test_topic_named_file_not_flagged(self, tmp_path):
        (tmp_path / "test_reporting_module_split.py").write_text("def test_x(): pass\n", encoding="utf-8")
        offenders = [str(p) for p in tmp_path.rglob("test_*.py") if any(pat.match(p.stem) for pat in _FORBIDDEN_PATTERNS)]
        assert not offenders

    def test_audit_finding_docstring_citation_not_flagged(self, tmp_path):
        # This project's own convention: docstrings cite "(audit: 04-High)"
        # freely -- this scanner only looks at FILENAMES, never at
        # docstring/comment text, so this must never be flagged.
        (tmp_path / "test_classify.py").write_text('"""Regression test (audit: 04-High)."""\ndef test_x(): pass\n', encoding="utf-8")
        offenders = [str(p) for p in tmp_path.rglob("test_*.py") if any(pat.match(p.stem) for pat in _FORBIDDEN_PATTERNS)]
        assert not offenders
