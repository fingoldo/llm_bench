"""BudgetGate unit tests — direct, no Benchmark/round_runner involved.

Added because BudgetGate previously had ZERO test coverage anywhere in
the repository (audit: 05-High) despite being the framework's only
spend-protection mechanism. Covers: happy path, skip-on-cap, the exact
boundary, headroom_factor, record_cost round-trip, the TOCTOU-race fix
(in-flight reservation), the storage-seed-once perf fix, and the
Stage.budget_per_call floor wiring.
"""

from __future__ import annotations

import asyncio

import pytest

from llm_bench import ExperimentTag, InMemoryStorage, RunRow
from llm_bench.runner.budget import BudgetGate, StageBudget

TAG = ExperimentTag("budget_test")


def _row(*, model: str = "m1", op: str = "enrich", cost: float = 0.10, eff_cost: float | None = None) -> RunRow:
    return RunRow(
        composite_hash=f"h_{model}_{op}_{cost}",
        provider="openrouter",
        model=model,
        thinking="",
        stage=op,
        task_unit_id="t1",
        stratum=None,
        lang=None,
        parent_prompt_hash=None,
        experiment_tag=TAG,
        response='{"ok": true, "padding": "enough length here"}',
        error_class=None,
        error_message=None,
        cost_usd=cost,
        effective_cost_usd=eff_cost if eff_cost is not None else cost,
        input_tokens=100,
        output_tokens=50,
        reasoning_tokens=0,
        cache_hit_tokens=0,
        cache_write_tokens=0,
    )


async def _store() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.initialize()
    return s


class TestUnbudgetedOp:
    async def test_no_budget_configured_always_ok(self):
        gate = BudgetGate()
        store = await _store()
        ok, spent, cap, reservation = await gate.check(store, TAG, "unconfigured_op")
        assert ok is True
        assert spent == 0.0
        assert cap == float("inf")
        assert reservation == 0.0


class TestHappyPathAndSkip:
    async def test_first_call_passes_and_reserves(self):
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=1.0)})
        store = await _store()
        ok, spent, cap, reservation = await gate.check(store, TAG, "enrich")
        assert ok is True
        assert spent == 0.0
        assert cap == 1.0
        # No last_call_cost_by_op data yet and no stage_budget_per_call
        # floor supplied -> falls back to the $0.01 minimum projection.
        assert reservation == pytest.approx(0.01)

    async def test_skips_when_over_cap(self):
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=0.005)})
        store = await _store()
        await store.record_call(_row(cost=0.10))
        ok, spent, cap, reservation = await gate.check(store, TAG, "enrich")
        assert ok is False
        assert reservation == 0.0
        assert spent == pytest.approx(0.10)

    async def test_exact_boundary_passes(self):
        # spent + projected == cap must NOT trip the gate (strict '>').
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=0.11, headroom_factor=1.0)})
        store = await _store()
        await store.record_call(_row(cost=0.10))
        await gate.record_cost(TAG, "enrich", 0.10)
        # last_call_cost_by_op["enrich"] = 0.10, headroom=1.0 -> projected=0.10
        # spent (0.10) + projected (0.10) = 0.20 > cap (0.11) -> should skip.
        # Recompute cap so spent+projected lands exactly on the boundary.
        gate2 = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=0.20, headroom_factor=1.0)})
        await gate2.record_cost(TAG, "enrich", 0.10)
        ok, spent, cap, reservation = await gate2.check(store, TAG, "enrich")
        assert ok is True  # 0.10 (spent) + 0.10 (projected) == 0.20 (cap) -> passes
        assert reservation == pytest.approx(0.10)

    async def test_headroom_factor_inflates_projection(self):
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=10.0, headroom_factor=3.0)})
        store = await _store()
        await gate.record_cost(TAG, "enrich", 0.50)
        _, _, _, reservation = await gate.check(store, TAG, "enrich")
        assert reservation == pytest.approx(1.50)  # 0.50 * 3.0


class TestBudgetPerCallFloor:
    async def test_stage_budget_per_call_used_before_first_real_cost(self):
        # Consumer declares Stage(..., budget_per_call=0.50) for an
        # expensive reasoning stage. Before any real call has happened,
        # the projection should reflect THAT, not a blind $0.01 floor
        # (audit: 03/09-High — Stage.budget_per_call was documented but
        # never actually read by the gate).
        gate = BudgetGate(budgets_by_op={"translate": StageBudget(cap_usd=10.0)})
        store = await _store()
        ok, _spent, _cap, reservation = await gate.check(
            store, TAG, "translate", stage_budget_per_call=0.50,
        )
        assert ok is True
        assert reservation == pytest.approx(0.50)

    async def test_floor_stops_applying_once_real_cost_known(self):
        gate = BudgetGate(budgets_by_op={"translate": StageBudget(cap_usd=10.0, headroom_factor=1.0)})
        store = await _store()
        await gate.record_cost(TAG, "translate", 0.05)
        # A real cost is now known ($0.05) — the stage_budget_per_call
        # floor (0.50) must NOT override the real, lower projection.
        _, _, _, reservation = await gate.check(
            store, TAG, "translate", stage_budget_per_call=0.50,
        )
        assert reservation == pytest.approx(0.05)


class TestRecordCostRoundtrip:
    async def test_record_cost_updates_last_call_cost(self):
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=10.0, headroom_factor=1.0)})
        await gate.record_cost(TAG, "enrich", 0.25)
        assert gate.last_call_cost_by_op["enrich"] == pytest.approx(0.25)

    async def test_record_cost_accumulates_spent_across_calls(self):
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=10.0)})
        store = await _store()
        await gate.check(store, TAG, "enrich")  # seeds spent=0.0 from storage
        await gate.record_cost(TAG, "enrich", 0.10)
        await gate.record_cost(TAG, "enrich", 0.20)
        spent = gate._spent_by_key[(str(TAG), "enrich")]
        assert spent == pytest.approx(0.30)

    async def test_record_cost_uses_effective_cost_for_running_total(self):
        # Cost-basis consistency fix (audit: 09-Medium): record_cost's
        # running "spent" total must track effective_cost_usd (the same
        # basis storage.query_spend_by_op aggregates), not raw cost_usd
        # — otherwise a stage with a meaningful cache discount has the
        # gate's own two halves silently disagreeing about "spent".
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=10.0)})
        store = await _store()
        await gate.check(store, TAG, "enrich")
        await gate.record_cost(TAG, "enrich", cost_usd=1.00, effective_cost_usd=0.10)
        spent = gate._spent_by_key[(str(TAG), "enrich")]
        assert spent == pytest.approx(0.10)

    async def test_record_cost_releases_reservation(self):
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=1.0, headroom_factor=1.0)})
        store = await _store()
        ok, _spent, _cap, reservation = await gate.check(store, TAG, "enrich")
        assert ok is True
        key = (str(TAG), "enrich")
        assert gate._reserved_by_key.get(key, 0.0) == pytest.approx(reservation)
        await gate.record_cost(TAG, "enrich", 0.02, reservation=reservation)
        assert gate._reserved_by_key.get(key, 0.0) == pytest.approx(0.0)

    async def test_record_cost_after_failed_call_still_releases_reservation(self):
        # A failed provider call still reaches record_cost with
        # cost_usd=0.0 (round_runner.py's contract) — the reservation
        # made by check() must be released regardless, or it stays
        # stuck forever, permanently shrinking future headroom.
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=1.0, headroom_factor=1.0)})
        store = await _store()
        _ok, _spent, _cap, reservation = await gate.check(store, TAG, "enrich")
        await gate.record_cost(TAG, "enrich", 0.0, reservation=reservation)
        assert gate._reserved_by_key[(str(TAG), "enrich")] == pytest.approx(0.0)
        assert gate._spent_by_key[(str(TAG), "enrich")] == pytest.approx(0.0)


class TestSeedOnceFromStorage:
    async def test_seeds_from_storage_exactly_once(self):
        # BudgetGate must query storage.query_spend_by_op ONCE per
        # (tag, op) it's ever asked about, not on every check() call
        # (audit: 06-High #3 — previously re-aggregated on every call,
        # measured 0.17ms -> 119ms as the tag grew).
        calls = {"n": 0}

        class _CountingStorage(InMemoryStorage):
            async def query_spend_by_op(self, *, experiment_tag):
                calls["n"] += 1
                return await super().query_spend_by_op(experiment_tag=experiment_tag)

        store = _CountingStorage()
        await store.initialize()
        await store.record_call(_row(cost=0.05))
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=10.0)})
        for _ in range(5):
            await gate.check(store, TAG, "enrich")
        assert calls["n"] == 1

    async def test_resumed_run_picks_up_real_prior_spend(self):
        store = await _store()
        await store.record_call(_row(cost=0.75))
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=1.0, headroom_factor=1.0)})
        ok, spent, _cap, _reservation = await gate.check(store, TAG, "enrich")
        assert spent == pytest.approx(0.75)
        assert ok is True  # 0.75 + 0.01 floor < 1.0


class TestConcurrentCheckDoesNotOvershoot:
    async def test_concurrent_checks_respect_shared_cap(self):
        # The TOCTOU-race fix (audit: 03/04/09-High): N concurrent
        # check() calls for the SAME op must not all see the same
        # not-yet-updated "spent" and all pass, collectively
        # overshooting cap_usd. Reproduces the audit's own repro
        # shape: 30 concurrent calls at $0.15 projected cost against a
        # $1.00 cap.
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=1.00, headroom_factor=1.0)})
        store = await _store()
        await gate.record_cost(TAG, "enrich", 0.15)  # seed a $0.15 projection

        async def _attempt() -> bool:
            ok, _spent, _cap, reservation = await gate.check(store, TAG, "enrich")
            if ok:
                await gate.record_cost(TAG, "enrich", 0.15, reservation=reservation)
            return ok

        results = await asyncio.gather(*(_attempt() for _ in range(30)))
        n_admitted = sum(results)
        actual_spend = n_admitted * 0.15
        # Before the fix: all 30 could pass (measured $4.50 vs a $1.00
        # cap in the audit's own repro). After: admission is bounded by
        # the cap, not by how many happened to race through together.
        assert actual_spend <= 1.00 + 1e-9
        assert n_admitted <= 7  # ceil(1.00 / 0.15) = 7 is the true ceiling
