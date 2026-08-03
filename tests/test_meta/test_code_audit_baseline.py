"""Meta-test: run pyutilz.dev.code_audit's generic AST/SQL scanners against
this repo's own source, baseline-driven per the project's snapshot-style
meta-test convention (see test_no_bare_except.py / test_api_stability.py).

Findings are baselined together (keyed by ``check::file:line``) so
pre-existing debt doesn't block adoption -- only a NEW finding fails the
test. Refresh with ``--refresh-code-audit-baseline`` after a deliberate
change, or add a narrow, commented exclusion in the ``exclude_dirs``
passed below for a confirmed false positive.

See ``pyutilz/src/pyutilz/dev/code_audit/__init__.py`` for what each check
catches (mutable defaults, late-binding closures, default-via-or traps,
silent broad-except swallows, logged-but-not-escalated excepts, SQL
LIMIT-without-ORDER-BY, OFFSET-pagination advisories, dead CLI flags,
non-idempotent SQL migrations, duplicate conditions/dict-keys, and
discarded coroutines).
"""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.code_audit_meta import assert_no_new_code_audit_findings

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench"
_BASELINE_PATH = Path(__file__).resolve().parent / "_code_audit_baseline.json"

_EXCLUDE_DIRS = frozenset({".benchmarks"})


def test_no_new_code_audit_findings(request):
    assert_no_new_code_audit_findings(
        root=_SRC_ROOT,
        baseline_path=_BASELINE_PATH,
        exclude_dirs=_EXCLUDE_DIRS,
        request=request,
    )
