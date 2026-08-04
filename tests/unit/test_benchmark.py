"""Benchmark.run_phase unit tests -- direct, non-integration exercises.

The existing end-to-end coverage of ``run_phase`` lives entirely under
``tests/integration/`` (excluded from the branch-coverage ratchet's
"not slow and not integration" filter), leaving the cascade's
all-candidates-eliminated early-halt branch with no unit-level test.
"""

from __future__ import annotations

from dataclasses import dataclass

from llm_bench import (
    Benchmark,
    HalvingSchedule,
    InMemoryStorage,
    Stage,
    StageGraph,
    TaskUnit,
)
from llm_bench.halving import Halving, RoundResult


def _pb(*, task_unit, ctx, lang=None):
    return (f"sys {task_unit.id}", "user")


def _parser(*, task_unit, response_text, input_tokens, output_tokens):
    if response_text is None:
        return None
    return {"raw": response_text}


@dataclass
class _FakePool:
    units: list[TaskUnit]

    def sample(self, n: int) -> list[TaskUnit]:
        return self.units[:n]

    def total_size(self) -> int:
        return len(self.units)


@dataclass
class _RecordingProvider:
    model: str

    async def generate(self, *, prompt: str, system: str) -> str:
        return '{"ok": true, "padding": "enough length here"}'


class TestCascadeHaltsWhenEveryCandidateIsEliminated:
    async def test_second_round_never_runs_after_full_elimination(self, monkeypatch):
        """When a round eliminates every candidate, the cascade must halt
        before the next round -- not crash trying to run a round with an
        empty candidate list. ``Halving.promote`` is monkeypatched to a
        deterministic "everyone eliminated" result -- reproducing this via
        organic provider failures is a real halving/coverage-gate
        interaction, not the thing this branch itself is about."""

        def _promote_eliminates_everyone(self, just_finished_round_idx, scores, eff_cost, **kwargs):
            return RoundResult(
                round_idx=just_finished_round_idx, stage="(aggregate)",
                candidates_in=list(scores), candidates_out=[],
                eliminated=list(scores), eliminated_reasons={m: "test-forced" for m in scores},
                scores=scores, effective_cost_per_call=eff_cost,
            )

        monkeypatch.setattr(Halving, "promote", _promote_eliminates_everyone)

        units = [TaskUnit(id="u0", stratum="v")]
        pool = _FakePool(units=units)
        stages = StageGraph([Stage(id="enrich", op="enrich", prompt_builder=_pb, parser=_parser)])
        schedule = HalvingSchedule(round_sizes=(2, 1), units_per_arm=(1, 1), pool_size=1)

        bench = Benchmark(
            task_pool=pool, stages=stages,
            storage=InMemoryStorage(),
            halving_schedule=schedule,
            provider_factory=lambda model: _RecordingProvider(model=model),
            global_concurrency=4,
        )

        async with bench:
            report = await bench.run_phase(
                tag="cascade_halt", candidates=["a", "b"], rounds=[1, 2],
            )

        # Only round 1 actually ran -- round 2 hit the "no candidates
        # left" halt and broke out of the loop instead of running.
        assert len(report.rounds) == 1
        assert report.final_candidates == []
