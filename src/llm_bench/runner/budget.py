"""BudgetGate — per-stage spend cap with skip-when-exhausted semantics.

Each stage has its own budget; when projected next-call cost would
breach the cap, the call is SKIPPED (not crashed). This is the same
gate VocabApp uses to protect Phase 1's $20 budget envelope from
runaway reasoning models.

The gate seeds its running total from the storage backend ONCE per
``(experiment_tag, op)`` pair it's ever asked to check (so a resumed
run picks up real prior spend), then tracks it incrementally in memory
— it does NOT re-query storage on every ``check()`` call (that used to
mean a full storage aggregation before every single budgeted LLM call;
measured 0.17ms -> 119ms as a FileStorage-backed tag grew to ~2000 rows
— audit: 06-High #3).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_bench.core.types import ExperimentTag
    from llm_bench.storage.base import BenchmarkStorage

logger = logging.getLogger(__name__)


@dataclass
class StageBudget:
    """Cap + headroom factor for one stage (or one op)."""
    cap_usd: float
    headroom_factor: float = 1.5
    """Projected next-call cost is multiplied by this before the gate
    check. >1.0 leaves room for a spike at the tail of the budget; the
    default 1.5 means a budget of $1 cuts off when the next call's
    projected cost + 1.5*last_cost > $1."""


@dataclass
class BudgetGate:
    """Per-op spend cap. Pre-call ``check`` returns the gate decision.

    ``budgets_by_op`` maps the canonical op name (``enrich``,
    ``translate``, ``validate_translate``, ...) to its ``StageBudget``.
    Stages without an entry get an unlimited budget (caller's choice).
    """
    budgets_by_op: dict[str, StageBudget] = field(default_factory=dict)
    last_call_cost_by_op: dict[str, float] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _spent_by_key: dict[tuple[str, str], float] = field(default_factory=dict, init=False, repr=False)
    _reserved_by_key: dict[tuple[str, str], float] = field(default_factory=dict, init=False, repr=False)
    _seeded_keys: set[tuple[str, str]] = field(default_factory=set, init=False, repr=False)

    async def check(
        self,
        storage: "BenchmarkStorage",
        experiment_tag: "ExperimentTag",
        op: str,
        *,
        stage_budget_per_call: float | None = None,
    ) -> tuple[bool, float, float, float]:
        """Returns ``(can_proceed, current_spend_plus_reserved, cap, reservation)``.

        ``can_proceed=False`` means the caller should SKIP the call
        (return a SkippedResult, no LLM spend, no DB row inserted) —
        ``reservation`` is ``0.0`` in that case.

        When ``can_proceed=True``, ``reservation`` is the projected cost
        this call just reserved against the cap; the caller MUST pass
        it back to ``record_cost(..., reservation=reservation)`` exactly
        once (win, loss, or parse failure — any outcome) so the
        reservation gets reconciled against the real cost instead of
        permanently over-counting the budget (audit: 03/04/09-High —
        the earlier version reserved nothing at all: N concurrent
        ``check()`` calls for the same ``op`` all saw the same
        not-yet-updated ``spent`` and could all pass simultaneously,
        overshooting ``cap_usd`` by up to N x the projected cost).

        ``stage_budget_per_call`` (from ``Stage.budget_per_call``) is
        used as a floor for the projection on the FIRST call of an
        ``op`` in this gate's lifetime, when there's no ``last_*``
        data yet to project from — previously the projection was a
        hardcoded ``$0.01`` floor regardless of what the caller
        declared as the stage's expected cost, so a consumer who wrote
        ``Stage(..., budget_per_call=0.50)`` got no protection at all
        for that first wave of calls (audit: 03/09-High).
        """
        budget = self.budgets_by_op.get(op)
        if budget is None:
            return True, 0.0, float("inf"), 0.0
        key = (str(experiment_tag), op)
        async with self._lock:
            if key not in self._seeded_keys:
                # First time THIS gate has been asked about this
                # (tag, op) pair — seed the running total from storage
                # once (a resumed run may already have real prior
                # spend recorded), then track incrementally from here.
                spent_by_op = await storage.query_spend_by_op(experiment_tag=experiment_tag)
                self._spent_by_key[key] = spent_by_op.get(op, 0.0)
                self._seeded_keys.add(key)
            spent = self._spent_by_key.get(key, 0.0)
            reserved = self._reserved_by_key.get(key, 0.0)
            last = self.last_call_cost_by_op.get(op, 0.0)
            floor = stage_budget_per_call if (last == 0.0 and stage_budget_per_call is not None) else 0.0
            projected = max(last * budget.headroom_factor, floor, 0.01)
            if spent + reserved + projected > budget.cap_usd:
                logger.warning(
                    "[budget] SKIP %s: spent=$%.4f + reserved=$%.4f + " "projected=$%.4f > cap=$%.4f",
                    op, spent, reserved, projected, budget.cap_usd,
                )
                return False, spent + reserved, budget.cap_usd, 0.0
            self._reserved_by_key[key] = reserved + projected
        return True, spent + reserved, budget.cap_usd, projected

    async def record_cost(
        self,
        experiment_tag: "ExperimentTag",
        op: str,
        cost_usd: float,
        *,
        effective_cost_usd: float | None = None,
        reservation: float = 0.0,
    ) -> None:
        """After a call this gate admitted completes (success, failure,
        OR parse failure — every outcome), record its REAL cost and
        release its reservation.

        Tracks the running total in ``effective_cost_usd`` terms — the
        SAME basis ``storage.query_spend_by_op`` aggregates in (see its
        docstring: "Group effective_cost_usd sum by op"). The earlier
        version recorded raw ``cost_usd`` here while ``check()``
        compared against ``effective_cost_usd``-based storage totals,
        so a stage with a meaningful cache discount had the two halves
        of the same gate silently disagreeing about what "spent" means
        — the gate under-estimated real spend and could SKIP a call
        that still had real headroom (audit: 09-Medium).
        """
        key = (str(experiment_tag), op)
        eff = effective_cost_usd if effective_cost_usd is not None else cost_usd
        async with self._lock:
            self.last_call_cost_by_op[op] = cost_usd
            self._spent_by_key[key] = self._spent_by_key.get(key, 0.0) + eff
            self._reserved_by_key[key] = max(0.0, self._reserved_by_key.get(key, 0.0) - reservation)
