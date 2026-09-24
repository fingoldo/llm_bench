"""Audit rounds under ``audits/`` stay countable by machine: ``py_ci_shared.audit_round_format``.

A round is countable when every ``### <ID> (...)`` finding carries a ``**Disposition:**`` and its tracker sits where its
rows say (open tree or ``audits/implemented/``). The one round this repo has, ``2026-07-22_full-audit``, predates that
format: ten category reports and an index table, no per-finding disposition and no ``TRACKER*.md``, so none of its 162
findings can be counted as closed or open. It is listed below as known debt; the list only shrinks, and any new round
must be countable from the day it lands.
"""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.audit_round_format import assert_rounds_filed, finding_problems, round_files

AUDITS = Path(__file__).resolve().parents[2] / "audits"

#: Rounds written before the countable format, each with what it lacks.
_UNCOUNTABLE_ROUNDS = {
    "2026-07-22_full-audit": "category reports plus 00-INDEX.md; findings have no **Disposition:** line and there is no tracker",
}


def test_rounds_are_countable() -> None:
    files = round_files(AUDITS)
    assert files, f"no round files under {AUDITS}; the layout moved or the glob broke"
    by_round: dict[str, list[Path]] = {}
    for path in files:
        by_round.setdefault(path.parent.name, []).append(path)
    problems: list[str] = []
    cache: dict[tuple[str, str], bool] = {}
    for name, paths in sorted(by_round.items()):
        results = [finding_problems(p, dir_cache=cache) for p in paths]
        if name in _UNCOUNTABLE_ROUNDS:
            if any(r is not None for r in results):
                problems.append(f"{name} now keeps dispositions; remove it from _UNCOUNTABLE_ROUNDS")
            continue
        if all(r is None for r in results):
            problems.append(f"{name}: no finding carries a **Disposition:**, so nothing in it can be counted")
        problems.extend(p for r in results if r for p in r)
    stale = sorted(set(_UNCOUNTABLE_ROUNDS) - set(by_round))
    problems.extend(f"{name} is listed in _UNCOUNTABLE_ROUNDS but no longer exists" for name in stale)
    assert not problems, "\n  ".join(["audit rounds that cannot be counted:", *problems])


def test_rounds_are_filed_where_their_trackers_say() -> None:
    assert_rounds_filed(
        AUDITS,
        known=[f"{name}: open round with no TRACKER*.md - its closure cannot be counted" for name in _UNCOUNTABLE_ROUNDS],
        min_trackers=0,
    )
