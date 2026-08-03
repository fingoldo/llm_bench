"""End-to-end smoke against ``InMemoryStorage`` with a fake provider.

Exercises the full ``Benchmark.run_phase`` cascade:
  - storage init + resume cache pre-load
  - per-round task-unit assignment
  - StageGraph topo execution per (model, task_unit)
  - resume cache hit / miss on repeated runs
  - ranking + per-stage winner selection
  - halving promote across rounds

No real LLM calls — a ``FakeProvider`` plugs in via ``provider_factory``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from llm_bench import (
    Benchmark,
    HalvingSchedule,
    InMemoryStorage,
    Stage,
    StageGraph,
    TaskUnit,
)

# ──────────────────────────────────────────────────────────────────────
# Test fakes
# ──────────────────────────────────────────────────────────────────────

@dataclass
class FakeProvider:
    """Stub LLM provider with deterministic per-(model) scoring.

    The fake is paired with a per-test ``model_quality`` map so each test
    can simulate "model_a is good at enrich, model_b is good at translate".
    Telemetry attributes mirror pyutilz's ``last_*`` fields.
    """
    model: str
    model_quality: dict[str, float]
    call_log: list[tuple[str, str]] = field(default_factory=list)
    last_input_tokens: int = 100
    last_output_tokens: int = 50
    last_reasoning_tokens: int = 0
    last_cost_usd: float = 0.001
    last_effective_cost_usd: float = 0.001
    last_cache_hit_tokens: int = 0
    last_cache_write_tokens: int = 0
    last_cache_discount_usd: float | None = None
    last_is_byok: bool | None = None
    last_web_search_citations: list[dict[str, Any]] | None = None
    last_upstream_resolved_model: str | None = None
    last_upstream_provider: str | None = None
    last_upstream_model: str | None = None
    last_native_finish_reason: str | None = "stop"
    last_generation_id: str | None = None

    async def generate(self, *, prompt: str, system: str) -> str:
        self.call_log.append((system[:30], prompt[:30]))
        # Cost varies by model — controls the cost-tiebreak ordering.
        cost = 0.001 if "cheap" in self.model else 0.005
        self.last_cost_usd = cost
        self.last_effective_cost_usd = cost
        quality = self.model_quality.get(self.model, 0.5)
        # Long enough (>= 20 chars) to clear default_row_scorer threshold.
        return '{"ok": true, "model": "%s", "quality": %.2f, "padding": "abcdefghij"}' % (self.model, quality)


def _build_prompt(*, task_unit, ctx, lang=None):
    parent_blob = ""
    if ctx.outputs:
        parent_blob = " | ".join(f"{sid}={out}" for sid, out in ctx.outputs.items())
    return (
        f"You are a benchmark stub. Stage tests {task_unit.id}.",
        f"Task: {task_unit.id}. Lang: {lang}. Parents: {parent_blob}",
    )


def _parser(*, task_unit, response_text, input_tokens, output_tokens):
    if response_text is None:
        return None
    return {"task_unit": task_unit.id, "raw": response_text}


@dataclass
class FakePool:
    units: list[TaskUnit]

    def sample(self, n: int) -> list[TaskUnit]:
        return self.units[:n]

    def total_size(self) -> int:
        return len(self.units)


@dataclass
class AsyncFakePool:
    """A TaskPool written exactly to the Protocol docstring's literal
    permission ("MAY be async-aware") -- both methods are coroutines,
    the shape that used to crash Benchmark._sample_units() with
    ``TypeError: object of type 'coroutine' has no len()`` (audit:
    04-High)."""
    units: list[TaskUnit]

    async def sample(self, n: int) -> list[TaskUnit]:
        return self.units[:n]

    async def total_size(self) -> int:
        return len(self.units)


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_smoke_run_phase_two_rounds():
    """Full happy path: 2 rounds, 3 candidates, 2 stages, 4 task units."""
    units = [TaskUnit(id=f"u{i}", metadata={"i": i}, stratum="v") for i in range(4)]
    pool = FakePool(units=units)
    stages = StageGraph([
        Stage(
            id="enrich", op="enrich",
            prompt_builder=_build_prompt, parser=_parser,
            budget_per_call=0.10,
        ),
        Stage(
            id="validate_enrich", op="validate_enrich",
            parent_stage="enrich", is_validator=True,
            prompt_builder=_build_prompt, parser=_parser,
            budget_per_call=0.05,
        ),
    ])
    candidates = ["cheap_a", "expensive_b", "cheap_c"]
    quality = {"cheap_a": 0.9, "expensive_b": 0.85, "cheap_c": 0.7}
    schedule = HalvingSchedule(
        round_sizes=(3, 2), units_per_arm=(2, 3), pool_size=4,
    )

    bench = Benchmark(
        task_pool=pool, stages=stages,
        storage=InMemoryStorage(),
        halving_schedule=schedule,
        provider_factory=lambda model: FakeProvider(
            model=model, model_quality=quality,
        ),
        global_concurrency=4,
    )

    async with bench:
        report = await bench.run_phase(
            tag="smoke_e2e", candidates=candidates,
        )

    assert len(report.rounds) == 2
    # Round 1: 3 candidates × 2 units = 6 enrich + 6 validate_enrich
    # Round 2: <=2 survivors × 3 units = up to 6 enrich + 6 validate_enrich
    assert report.final_ranking is not None
    assert "enrich" in report.final_ranking.stages
    assert "validate_enrich" in report.final_ranking.stages
    # All candidates produced rows in round 1.
    enrich_rows = report.final_ranking.stages["enrich"].rows
    enrich_models = {r.model for r in enrich_rows}
    assert candidates[0] in enrich_models
    # Round-2 survivors persisted.
    assert 0 < len(report.final_candidates) <= 2


@pytest.mark.asyncio
async def test_smoke_run_phase_with_async_task_pool():
    # Direct regression test for the async-TaskPool crash (audit:
    # 04-High): a pool whose sample()/total_size() are coroutines, per
    # the Protocol docstring's own documented permission, must work
    # exactly like a synchronous pool -- not crash on `len(coroutine)`.
    units = [TaskUnit(id=f"u{i}", metadata={"i": i}, stratum="v") for i in range(2)]
    pool = AsyncFakePool(units=units)
    stages = StageGraph([
        Stage(id="enrich", op="enrich", prompt_builder=_build_prompt, parser=_parser),
    ])
    schedule = HalvingSchedule(round_sizes=(2,), units_per_arm=(2,), pool_size=2)

    bench = Benchmark(
        task_pool=pool, stages=stages,
        storage=InMemoryStorage(),
        halving_schedule=schedule,
        provider_factory=lambda model: FakeProvider(model=model, model_quality={"a": 0.9, "b": 0.8}),
    )
    async with bench:
        report = await bench.run_phase(tag="async_pool_e2e", candidates=["a", "b"])

    assert len(report.rounds) == 1
    assert report.final_ranking is not None


@pytest.mark.asyncio
async def test_smoke_resume_cache_hits_on_rerun():
    """Re-running the same tag finds every prior call in cache (zero new spend)."""
    units = [TaskUnit(id="u0", metadata={}, stratum="v")]
    pool = FakePool(units=units)
    stages = StageGraph([
        Stage(
            id="enrich", op="enrich",
            prompt_builder=_build_prompt, parser=_parser,
        ),
    ])
    schedule = HalvingSchedule(
        round_sizes=(2,), units_per_arm=(1,), pool_size=1,
    )
    candidates = ["cheap_a", "cheap_b"]
    quality = {"cheap_a": 0.9, "cheap_b": 0.8}
    storage = InMemoryStorage()

    call_logs: list[list[tuple[str, str]]] = []

    def factory(model: str) -> FakeProvider:
        p = FakeProvider(model=model, model_quality=quality)
        call_logs.append(p.call_log)
        return p

    bench = Benchmark(
        task_pool=pool, stages=stages, storage=storage,
        halving_schedule=schedule, provider_factory=factory,
    )

    async with bench:
        await bench.run_phase(tag="resume_t", candidates=candidates)

    first_run_total_calls = sum(len(log) for log in call_logs)
    assert first_run_total_calls > 0

    # Second run, same tag — cache should serve every call.
    call_logs.clear()
    bench2 = Benchmark(
        task_pool=pool, stages=stages, storage=storage,
        halving_schedule=schedule, provider_factory=factory,
    )
    async with bench2:
        await bench2.run_phase(tag="resume_t", candidates=candidates)
    second_run_total_calls = sum(len(log) for log in call_logs)
    assert second_run_total_calls == 0, f"resume cache should cover every call, got {second_run_total_calls} fresh calls"


@pytest.mark.asyncio
async def test_smoke_winner_persisted_per_round():
    """After round k, ``load_winners(tag, round_idx=k)`` returns a WinnerSet."""
    units = [TaskUnit(id="u0", stratum="v"), TaskUnit(id="u1", stratum="v")]
    pool = FakePool(units=units)
    stages = StageGraph([
        Stage(
            id="enrich", op="enrich",
            prompt_builder=_build_prompt, parser=_parser,
        ),
    ])
    schedule = HalvingSchedule(
        round_sizes=(3,), units_per_arm=(2,), pool_size=2,
    )
    storage = InMemoryStorage()
    quality = {"a": 0.9, "b": 0.7, "c": 0.5}
    bench = Benchmark(
        task_pool=pool, stages=stages, storage=storage,
        halving_schedule=schedule,
        provider_factory=lambda m: FakeProvider(model=m, model_quality=quality),
    )
    async with bench:
        await bench.run_phase(tag="winners_t", candidates=["a", "b", "c"])

    from llm_bench.core.types import ExperimentTag
    ws = await storage.load_winners(
        experiment_tag=ExperimentTag("winners_t"), round_idx=1,
    )
    assert ws is not None
    assert "enrich" in ws.winners
    # The winner is one of the candidates we ran.
    assert ws.winners["enrich"] in {"a", "b", "c"}
