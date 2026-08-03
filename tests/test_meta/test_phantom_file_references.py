"""Catch dead relative-path mentions in markdown-link syntax.

Motivation: the audit found README.md linking to ``docs/architecture.md``
before that file existed (fixed since). Scans every markdown-link target
(``[text](path/to/thing.ext)``) in every .md file under the repo, and
verifies each target resolves either against the repo root or against
the referencing file's own directory.

Deliberately scoped to (a) markdown-LINK syntax only, not bare filename
mentions in prose (e.g. "see round_runner.py") -- bare mentions are
extremely common as plain module-naming in docstrings/comments and
produce a very high false-positive rate without a much more
sophisticated context model -- and (b) .md files only, not .py source:
scanning raw .py source text for this pattern picks up unrelated
string literals (test fixture data that happens to contain
"[x](y.py)"-shaped content) far more often than it catches a real dead
doc link. Markdown links in prose docs are an explicit, low-noise claim
that a target resolves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# [text](path/to/thing.py) -- captures the link target.
_MD_LINK_RE = re.compile(r"\]\(([\w./-]+\.(?:py|md|sql|yml|yaml|json))\)")

# "<rel_path>::<matched target>" entries skipped as confirmed false positives.
_WHITELIST: set[str] = set()


def _is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "git+"))


def _resolves(target: str, referencing_file: Path) -> bool:
    if (_REPO_ROOT / target).exists():
        return True
    if (referencing_file.parent / target).exists():
        return True
    return False


def _scan_file(path: Path, rel: str) -> list[str]:
    violations: list[str] = []
    source = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(source.splitlines(), start=1):
        for m in _MD_LINK_RE.finditer(line):
            target = m.group(1)
            if _is_external(target):
                continue
            key = f"{rel}::{target}"
            if key in _WHITELIST:
                continue
            if not _resolves(target, path):
                violations.append(f"{rel}:{lineno}: dead markdown-link target {target!r}")
    return violations


def _all_violations() -> list[str]:
    # Scoped to .md files only: .py files' markdown-bracket-paren syntax
    # almost never appears outside docstrings, and scanning raw source
    # text picks up unrelated string literals (e.g. test fixture data
    # that happens to contain "[x](y.py)"-shaped content) far more often
    # than it catches a real dead doc link.
    out: list[str] = []
    for path in sorted(_REPO_ROOT.glob("*.md")):
        out.extend(_scan_file(path, str(path.relative_to(_REPO_ROOT))))
    docs_dir = _REPO_ROOT / "docs"
    if docs_dir.exists():
        for path in sorted(docs_dir.rglob("*.md")):
            out.extend(_scan_file(path, str(path.relative_to(_REPO_ROOT))))
    return out


def test_no_phantom_markdown_links():
    violations = _all_violations()
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"Dead markdown-link target(s):\n  {msg}")


class TestPhantomFileReferencesDetectsShapes:
    def test_dead_markdown_link_detected(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("See [architecture](docs/does_not_exist_xyz.md) for details.\n", encoding="utf-8")
        violations = _scan_file(readme, "README.md")
        assert len(violations) == 1

    def test_existing_link_target_not_flagged(self, tmp_path):
        (tmp_path / "helper.md").write_text("notes\n", encoding="utf-8")
        readme = tmp_path / "README.md"
        readme.write_text("See [notes](helper.md) for details.\n", encoding="utf-8")
        violations = _scan_file(readme, "README.md")
        assert violations == []

    def test_external_url_link_not_flagged(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("See [changelog](https://keepachangelog.com/setup.md) for format.\n", encoding="utf-8")
        violations = _scan_file(readme, "README.md")
        assert violations == []

    def test_prose_without_link_syntax_not_flagged(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("Configured in round_runner.py and docs/architecture.md.\n", encoding="utf-8")
        violations = _scan_file(readme, "README.md")
        assert violations == []
