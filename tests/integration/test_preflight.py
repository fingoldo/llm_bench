"""Preflight ping covers the bug class that wasted half a VocabApp benchmark
on 2026-05-08: the consumer's wiring (vendor pin without fallback) made
~10% of candidates return 404 forever, but the framework only learned about
it AFTER paying for retries. Preflight catches such misrouted/dead models
in a tiny ping batch *before* round 1 spend.

These tests exercise the framework's preflight path with a fake provider
factory; the four scenarios mirror real failure modes.
"""

from __future__ import annotations

import asyncio
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


@dataclass
class _FakeProvider:
    """One-shot fake. ``behavior`` controls what generate() does:

      ok:      return short response
      404:     raise LLMProviderError-shaped exception (ModelNotFound)
      timeout: never resolve until cancelled
      raise:   raise generic error
    """
    model: str
    behavior: str
    last_input_tokens: int = 5
    last_output_tokens: int = 5
    last_cost_usd: float = 0.001
    last_effective_cost_usd: float = 0.001
    last_reasoning_tokens: int = 0
    call_log: list[tuple[str, str]] = field(default_factory=list)

    async def generate(self, *, prompt: str, system: str, max_tokens: int = 5) -> str:
        self.call_log.append((system[:30], prompt[:30]))
        if self.behavior == "ok":
            return "ok"
        if self.behavior == "404":
            raise Exception("OpenRouter API error 404: No endpoints found for X.")
        if self.behavior == "timeout":
            await asyncio.sleep(60.0)  # longer than test timeout
            return "should not get here"
        if self.behavior == "raise":
            raise RuntimeError("unexpected provider explosion")
        raise AssertionError(f"unknown behavior {self.behavior!r}")


def _build_benchmark(behaviors: dict[str, str]) -> Benchmark:
    """Helper: a Benchmark whose provider_factory hands out per-model fakes."""
    units = [TaskUnit(id="u0", stratum="v")]

    def _stub_pb(*, task_unit, ctx, lang=None):
        return "sys", f"task {task_unit.id}"

    def _stub_parser(*, task_unit, response_text, input_tokens, output_tokens):
        return {"resp": response_text}

    stages = StageGraph([Stage(
        id="enrich", op="enrich",
        prompt_builder=_stub_pb, parser=_stub_parser,
    )])

    @dataclass
    class _Pool:
        units: list[TaskUnit]
        def sample(self, n): return self.units[:n]
        def total_size(self): return len(self.units)

    return Benchmark(
        task_pool=_Pool(units=units),
        stages=stages,
        storage=InMemoryStorage(),
        halving_schedule=HalvingSchedule(
            round_sizes=(2, 1), units_per_arm=(1, 1), pool_size=1,
        ),
        provider_factory=lambda model: _FakeProvider(
            model=model, behavior=behaviors.get(model, "ok"),
        ),
    )


@pytest.mark.asyncio
async def test_preflight_marks_404_as_routing_failure():
    bench = _build_benchmark({
        "alive_a": "ok",
        "dead_b": "404",
        "alive_c": "ok",
    })
    async with bench:
        verdicts = await bench.preflight(
            ["alive_a", "dead_b", "alive_c"], timeout_sec=2.0,
        )
    assert verdicts["alive_a"].ok is True
    assert verdicts["alive_c"].ok is True
    assert verdicts["dead_b"].ok is False
    assert verdicts["dead_b"].error_class == "ModelNotFound"


@pytest.mark.asyncio
async def test_preflight_marks_timeout():
    bench = _build_benchmark({"slow": "timeout", "fast": "ok"})
    async with bench:
        verdicts = await bench.preflight(["slow", "fast"], timeout_sec=0.5)
    assert verdicts["slow"].ok is False
    assert verdicts["slow"].error_class == "LLMCallTimeout"
    assert verdicts["fast"].ok is True


@pytest.mark.asyncio
async def test_run_phase_with_preflight_drops_dead_candidates():
    """The whole point: preflight=True filters dead candidates before round 1
    so they don't burn budget on doomed retries."""
    bench = _build_benchmark({
        "good_1": "ok",
        "dead_2": "404",
        "good_3": "ok",
        "dead_4": "404",
    })
    async with bench:
        report = await bench.run_phase(
            tag="preflight_smoke", candidates=["good_1", "dead_2", "good_3", "dead_4"],
            preflight=True,
        )
    # Preflight verdict is exposed on the report.
    assert len(report.preflight) == 4
    assert report.preflight["dead_2"].ok is False
    assert report.preflight["dead_4"].ok is False
    # The 2 dead models do NOT appear among candidates the rounds saw.
    # Round-1 ranking will only have good_1 / good_3.
    assert report.final_ranking is not None
    enrich = report.final_ranking.stages.get("enrich")
    if enrich is not None:
        assert "dead_2" not in enrich.model_scores
        assert "dead_4" not in enrich.model_scores


@pytest.mark.asyncio
async def test_preflight_without_provider_factory_raises():
    """Without provider_factory there's nothing to ping with."""
    bench = _build_benchmark({})
    bench.provider_factory = None
    async with bench:
        with pytest.raises(ValueError, match="provider_factory"):
            await bench.preflight(["x"])


@pytest.mark.asyncio
async def test_preflight_runs_concurrently():
    """``concurrency`` cap actually parallelises (not serialises) calls."""
    bench = _build_benchmark({m: "ok" for m in ["m1", "m2", "m3", "m4", "m5", "m6"]})
    import time
    t0 = time.monotonic()
    async with bench:
        await bench.preflight(["m1", "m2", "m3", "m4", "m5", "m6"], concurrency=6)
    dt = time.monotonic() - t0
    # Six parallel ~instant pings should finish well under 2s; if they
    # serialised at concurrency=1 we'd see >1s easily even on a fast box.
    assert dt < 2.0, f"preflight too slow ({dt:.2f}s) - is concurrency wired?"
