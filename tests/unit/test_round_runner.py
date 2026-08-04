"""round_runner.py unit tests — direct RoundConfig/run_round exercises.

The 386-line orchestration core previously had NO direct unit test
anywhere (audit: 05-High) — only transitive "happy path" coverage via
Benchmark.run_phase smoke tests whose fakes never raise / never return
malformed output. These tests target the specific Critical/High bugs
found and fixed in round_runner.py directly: M_X winner-substrate
wiring, validator independence, the resume-cache-hit coverage-gate
fix, parse-failure caching, the 3-DEAD-errors circuit breaker,
per_op_concurrency, budget-gate skip, provider_label, and infra-
failure surfacing into RoundResult.notes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from llm_bench import (
    ExperimentTag,
    Halving,
    HalvingSchedule,
    InMemoryStorage,
    RunRow,
    Stage,
    StageGraph,
    TaskUnit,
)
from llm_bench.runner.budget import BudgetGate, StageBudget
from llm_bench.runner.resume import ResumeCache
from llm_bench.runner.round_runner import RoundConfig, _build_prompt, _parse_safely, run_round
from llm_bench.stage.base import StageContext

TAG = ExperimentTag("rr_test")


def _pb(*, task_unit, ctx, lang=None):
    parent = ctx.outputs.get("root")
    return (f"sys for {task_unit.id}", f"user parent={parent!r}")


def _parser(*, task_unit, response_text, input_tokens, output_tokens):
    if response_text is None:
        return None
    return {"raw": response_text}


def _graph_single_stage(op: str = "enrich", **kwargs) -> StageGraph:
    return StageGraph([Stage(id="root", op=op, prompt_builder=_pb, parser=_parser, **kwargs)])


def _units(n: int) -> list[TaskUnit]:
    return [TaskUnit(id=f"u{i}", stratum="v") for i in range(n)]


def _cfg(
    *, candidates, task_units, stages, storage=None, round_idx=1,
    schedule=None, budget_gate=None, provider_factory=None,
    provider_label="openrouter", global_concurrency=30,
    per_op_concurrency=None, halving=None,
) -> RoundConfig:
    storage = storage or InMemoryStorage()
    schedule = schedule or HalvingSchedule(round_sizes=(len(candidates),), units_per_arm=(len(task_units),), pool_size=len(task_units))
    halving = halving if halving is not None else Halving(schedule=schedule)
    return RoundConfig(
        experiment_tag=TAG,
        round_idx=round_idx,
        candidates=candidates,
        task_units=task_units,
        stages=stages,
        schedule=schedule,
        storage=storage,
        halving=halving,
        resume_cache=ResumeCache(),
        budget_gate=budget_gate,
        provider_factory=provider_factory,
        provider_label=provider_label,
        global_concurrency=global_concurrency,
        per_op_concurrency=per_op_concurrency or {},
    )


async def _init(storage: InMemoryStorage) -> InMemoryStorage:
    await storage.initialize()
    return storage


# ──────────────────────────────────────────────────────────────────────
# Providers
# ──────────────────────────────────────────────────────────────────────

@dataclass
class _RecordingProvider:
    model: str
    call_log: list[str]
    response_text: str = '{"ok": true, "padding": "enough length here"}'
    last_input_tokens: int = 10
    last_output_tokens: int = 5
    last_reasoning_tokens: int = 0
    last_cost_usd: float = 0.01
    last_effective_cost_usd: float = 0.01

    async def generate(self, *, prompt: str, system: str) -> str:
        self.call_log.append(self.model)
        return self.response_text


@dataclass
class _AlwaysFailProvider:
    model: str
    call_log: list[str]

    async def generate(self, *, prompt: str, system: str) -> str:
        self.call_log.append(self.model)
        raise RuntimeError("OpenRouter API error 404: No endpoints found for x.")


# ──────────────────────────────────────────────────────────────────────
# Winner-substrate wiring (Critical)
# ──────────────────────────────────────────────────────────────────────

class TestWinnerSubstrateWiring:
    async def test_round2_uses_round1_winner_substrate_not_own_output(self):
        seen_parents: dict[str, list[str]] = {"a": [], "b": []}

        def child_pb(*, task_unit, ctx, lang=None):
            parent = ctx.outputs.get("root")
            return (f"sys {task_unit.id}", f"user {parent}")

        def child_parser(*, task_unit, response_text, input_tokens, output_tokens):
            return {"raw": response_text} if response_text else None

        def root_pb(*, task_unit, ctx, lang=None):
            return ("sys root", f"user root {task_unit.id}")

        graph = StageGraph([
            Stage(id="root", op="root", prompt_builder=root_pb, parser=_parser),
            Stage(id="child", op="child", parent_stage="root", prompt_builder=child_pb, parser=child_parser),
        ])
        storage = await _init(InMemoryStorage())
        units = _units(1)
        schedule = HalvingSchedule(round_sizes=(2, 2), units_per_arm=(1, 1), pool_size=1)
        halving = Halving(schedule=schedule)

        # Round 1: "a" and "b" produce DIFFERENT root outputs. "a" is
        # engineered to win via cost (cheaper), so round 2 should use
        # "a"'s root output as the substrate for BOTH candidates' child
        # stage — not each candidate's own root output.
        class _RootAwareProvider:
            def __init__(self, model):
                self.model = model
                self.last_input_tokens = 10
                self.last_output_tokens = 5
                self.last_reasoning_tokens = 0
                self.last_cost_usd = 0.001 if model == "a" else 0.5
                self.last_effective_cost_usd = self.last_cost_usd

            async def generate(self, *, prompt, system):
                if "sys root" in system:
                    return f'{{"ok": true, "who": "{self.model}", "padding": "enough len"}}'
                # child stage: record what parent value this call saw.
                seen_parents[self.model].append(prompt)
                return '{"ok": true, "padding": "enough length here too"}'

        cfg1 = _cfg(
            candidates=["a", "b"], task_units=units, stages=graph, storage=storage,
            round_idx=1, schedule=schedule, halving=halving,
            provider_factory=_RootAwareProvider,
        )
        await run_round(cfg1)
        # Round 1 has no prior winner (round_idx == 1), so each
        # candidate's own child call legitimately sees its OWN root
        # output there — clear before round 2 so the assertion below
        # only inspects round-2 behaviour, where the substrate SHOULD
        # apply.
        seen_parents["a"].clear()
        seen_parents["b"].clear()

        cfg2 = _cfg(
            candidates=["a", "b"], task_units=units, stages=graph, storage=storage,
            round_idx=2, schedule=schedule, halving=halving,
            provider_factory=_RootAwareProvider,
        )
        await run_round(cfg2)

        # Both candidates' round-2 child-stage prompt must reference the
        # WINNER's ("a"'s) root output, not "b"'s own root output.
        assert seen_parents["a"], "candidate a's child stage never ran in round 2"
        assert seen_parents["b"], "candidate b's child stage never ran in round 2"
        for prompt in seen_parents["a"] + seen_parents["b"]:
            assert '"who": "a"' in prompt, f"expected winner a's substrate, got: {prompt}"

    async def test_round1_has_no_substrate_uses_own_output(self):
        # Sanity check: round 1 has no prior winner, so a candidate's
        # child stage must see ITS OWN root output (the fallback path).
        seen: list[str] = []

        def child_pb(*, task_unit, ctx, lang=None):
            return ("sys child", f"parent={ctx.outputs.get('root')}")

        graph = StageGraph([
            Stage(id="root", op="root", prompt_builder=lambda **kw: ("sys root", "u"), parser=_parser),
            Stage(id="child", op="child", parent_stage="root", prompt_builder=child_pb, parser=_parser),
        ])
        storage = await _init(InMemoryStorage())

        class _P:
            def __init__(self, model):
                self.model = model
                self.last_input_tokens = 1
                self.last_output_tokens = 1
                self.last_reasoning_tokens = 0
                self.last_cost_usd = 0.001
                self.last_effective_cost_usd = 0.001

            async def generate(self, *, prompt, system):
                if system == "sys root":
                    return f'{{"who": "{self.model}", "padding": "enough length here"}}'
                seen.append(prompt)
                return '{"ok": true, "padding": "enough length here too"}'

        cfg = _cfg(candidates=["solo"], task_units=_units(1), stages=graph, storage=storage, provider_factory=_P)
        await run_round(cfg)
        # _parser wraps the raw response text under "raw" (it doesn't
        # itself parse JSON) — the child stage should see exactly what
        # THIS candidate's own root call returned, unmodified.
        assert seen == ["""parent={'raw': '{"who": "solo", "padding": "enough length here"}'}"""]


# ──────────────────────────────────────────────────────────────────────
# mean_latency_sec reaches the halving-cut decision, not just the
# per-stage winner pick (audit: 02-High / 03-Medium)
# ──────────────────────────────────────────────────────────────────────

class TestLatencyReachesHalvingCut:
    async def test_faster_candidate_wins_score_and_cost_tie(self):
        # Two candidates tied on score AND eff_cost; only latency
        # differs. This end-to-end run_round() test only passes if
        # round_runner.py actually aggregates per-model latency and
        # forwards it into Halving.promote()'s mean_latency_sec= param
        # -- cost_tiebreak_key's own latency math is unit-tested in
        # isolation elsewhere (test_halving.py), but nothing previously
        # exercised the wiring between the two.
        class _TimedProvider:
            def __init__(self, model, delay):
                self.model = model
                self.delay = delay
                self.last_input_tokens = 10
                self.last_output_tokens = 5
                self.last_reasoning_tokens = 0
                self.last_cost_usd = 0.01
                self.last_effective_cost_usd = 0.01

            async def generate(self, *, prompt, system):
                if self.delay:
                    await asyncio.sleep(self.delay)
                return '{"ok": true, "padding": "enough length here"}'

        graph = _graph_single_stage()
        storage = await _init(InMemoryStorage())
        schedule = HalvingSchedule(round_sizes=(2, 1), units_per_arm=(1, 1), pool_size=1)

        def factory(m):
            return _TimedProvider(m, delay=0.05 if m == "slow" else 0.0)

        cfg = _cfg(
            candidates=["fast", "slow"], task_units=_units(1), stages=graph, storage=storage,
            schedule=schedule, provider_factory=factory,
        )
        result = await run_round(cfg)
        assert result.candidates_out == ["fast"]


# ──────────────────────────────────────────────────────────────────────
# Multi-specialty score bonus carries into the NEXT round's decision
# ──────────────────────────────────────────────────────────────────────

class TestMultiSpecialtyBonusAppliesNextRound:
    async def test_bonus_earned_round1_flips_round2_survivor(self):
        # "b" wins BOTH stages in round 1 (>=2 wins -> +0.05 bonus,
        # halving/driver.py). Round 1 doesn't eliminate anyone (pool=2,
        # target=2), so the cut that matters is round 2's, sized so
        # exactly 1 of {"a","b"} survives. Round-2 raw scores are set
        # so "a" > "b" WITHOUT the bonus but "b" > "a" WITH it applied
        # -- this only passes if round_runner.py actually reads
        # halving.last_score_bonus back into round 2's aggregate scores
        # before calling promote() again (audit: 03-Medium).
        current_round = {"n": 1}

        def scorer(row):
            table = {
                1: {"a": 0.50, "b": 0.90},
                2: {"a": 0.80, "b": 0.76},
            }
            return table[current_round["n"]][row.model]

        graph = StageGraph([
            Stage(id="s1", op="s1", prompt_builder=lambda **kw: ("s", "u1"), parser=_parser),
            Stage(id="s2", op="s2", prompt_builder=lambda **kw: ("s", "u2"), parser=_parser),
        ])
        storage = await _init(InMemoryStorage())
        # round_sizes=(2, 2): round 2 is the FINAL round (just_finished
        # == len(round_sizes)), which skips specialty-preservation
        # force-promotion entirely -- otherwise round 2's OWN per-stage
        # winner ("a", since its raw score beats "b"'s raw score before
        # the bonus is applied) would get force-promoted back in
        # regardless of the halving cut, masking the exact bonus-carry
        # signal this test targets.
        schedule = HalvingSchedule(round_sizes=(2, 2), units_per_arm=(1, 1), pool_size=1)
        halving = Halving(schedule=schedule)
        call_log: list[str] = []

        cfg1 = _cfg(
            candidates=["a", "b"], task_units=_units(1), stages=graph, storage=storage,
            schedule=schedule, halving=halving, round_idx=1,
            provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        cfg1.row_scorer = scorer
        result1 = await run_round(cfg1)
        assert set(result1.candidates_out) == {"a", "b"}  # trivial round-1 cut, both survive
        assert halving.last_score_bonus.get("b") == pytest.approx(0.05)

        current_round["n"] = 2
        cfg2 = _cfg(
            candidates=["a", "b"], task_units=_units(1), stages=graph, storage=storage,
            schedule=schedule, halving=halving, round_idx=2,
            provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        cfg2.row_scorer = scorer
        result2 = await run_round(cfg2)

        # Raw round-2 scores alone would promote "a" (0.80 > 0.76); the
        # carried-over bonus must be what flips it to "b" (0.76+0.05=0.81 > 0.80).
        assert result2.candidates_out == ["b"]


# ──────────────────────────────────────────────────────────────────────
# Validator independence (Critical)
# ──────────────────────────────────────────────────────────────────────

class TestValidatorIndependence:
    async def test_validator_stage_answered_by_different_model(self):
        answered_by: dict[str, str] = {}

        def draft_pb(*, task_unit, ctx, lang=None):
            return ("sys draft", "u draft")

        def validate_pb(*, task_unit, ctx, lang=None):
            return ("sys validate", "u validate")

        graph = StageGraph([
            Stage(id="draft", op="draft", prompt_builder=draft_pb, parser=_parser),
            Stage(id="validate", op="validate", parent_stage="draft", is_validator=True, prompt_builder=validate_pb, parser=_parser),
        ])
        storage = await _init(InMemoryStorage())

        class _IdentifyingProvider:
            def __init__(self, model):
                self.model = model
                self.last_input_tokens = 1
                self.last_output_tokens = 1
                self.last_reasoning_tokens = 0
                self.last_cost_usd = 0.001
                self.last_effective_cost_usd = 0.001

            async def generate(self, *, prompt, system):
                if system == "sys validate":
                    # Record which model instance actually answered.
                    answered_by[self.model] = self.model
                    return f'{{"answered_by": "{self.model}", "padding": "enough length"}}'
                return '{"ok": true, "padding": "enough length here too"}'

        cfg = _cfg(
            candidates=["m1", "m2", "m3"], task_units=_units(1), stages=graph,
            storage=storage, provider_factory=_IdentifyingProvider,
        )
        await run_round(cfg)

        rows = [r async for r in storage.query_rows(experiment_tag=TAG, stage="validate")]
        assert len(rows) == 3
        for row in rows:
            # row.model is the PRODUCER; the response must have been
            # generated by a DIFFERENT model (self-validation forbidden).
            assert f'"answered_by": "{row.model}"' not in (row.response or ""), f"producer {row.model} appears to have validated its own output: {row.response}"
            assert row.generation_details.get("validator_model") != row.model

    async def test_two_candidates_skips_validator_stage_entirely(self):
        graph = StageGraph([
            Stage(id="draft", op="draft", prompt_builder=lambda **kw: ("s", "u"), parser=_parser),
            Stage(id="validate", op="validate", parent_stage="draft", is_validator=True, prompt_builder=lambda **kw: ("s2", "u2"), parser=_parser),
        ])
        storage = await _init(InMemoryStorage())
        call_log: list[str] = []
        cfg = _cfg(
            candidates=["m1", "m2"], task_units=_units(1), stages=graph, storage=storage,
            provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        await run_round(cfg)
        validate_rows = [r async for r in storage.query_rows(experiment_tag=TAG, stage="validate")]
        assert validate_rows == []
        # But draft rows DID run for both.
        draft_rows = [r async for r in storage.query_rows(experiment_tag=TAG, stage="draft")]
        assert len(draft_rows) == 2

    async def test_two_candidates_validator_skip_does_not_break_coverage_gate(self):
        # The N<3 validator skip must count as "attempted" for coverage
        # purposes (a pool-wide structural limitation, not any one
        # candidate's fault) — otherwise every candidate gets wrongly
        # eliminated by its own coverage gate for a stage nobody in the
        # pool could have run.
        graph = StageGraph([
            Stage(id="draft", op="draft", prompt_builder=lambda **kw: ("s", "u"), parser=_parser),
            Stage(id="validate", op="validate", parent_stage="draft", is_validator=True, prompt_builder=lambda **kw: ("s2", "u2"), parser=_parser),
        ])
        storage = await _init(InMemoryStorage())
        call_log: list[str] = []
        schedule = HalvingSchedule(round_sizes=(2, 2), units_per_arm=(1, 1), pool_size=1)
        cfg = _cfg(
            candidates=["m1", "m2"], task_units=_units(1), stages=graph, storage=storage,
            schedule=schedule, provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        result = await run_round(cfg)
        assert set(result.candidates_out) == {"m1", "m2"}


# ──────────────────────────────────────────────────────────────────────
# Resume-cache-hit coverage-gate fix (Critical)
# ──────────────────────────────────────────────────────────────────────

class TestResumeCoverageGate:
    async def test_fully_resumed_round_does_not_eliminate_everyone(self):
        storage = await _init(InMemoryStorage())
        graph = _graph_single_stage()
        schedule = HalvingSchedule(round_sizes=(2, 2), units_per_arm=(1, 1), pool_size=1)
        call_log: list[str] = []

        cfg1 = _cfg(
            candidates=["a", "b"], task_units=_units(1), stages=graph, storage=storage,
            schedule=schedule, provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        result1 = await run_round(cfg1)
        assert set(result1.candidates_out) == {"a", "b"}

        # Resume: fresh ResumeCache pre-populated from storage (as
        # Benchmark.initialize() would do), same tag/round -> should be
        # a 100% cache-hit round with ZERO new provider calls, and must
        # NOT eliminate the fully-succeeded candidates via the coverage
        # gate (audit: Critical, reproduced independently by 2 reports).
        call_log.clear()
        resume_cache = ResumeCache()
        await resume_cache.populate_from_storage(storage)
        cfg2 = _cfg(
            candidates=["a", "b"], task_units=_units(1), stages=graph, storage=storage,
            schedule=schedule, provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        cfg2.resume_cache = resume_cache
        result2 = await run_round(cfg2)
        assert call_log == []
        assert set(result2.candidates_out) == {"a", "b"}
        for reason in result2.eliminated_reasons.values():
            assert "coverage_min" not in reason

    async def test_coverage_denominator_scales_by_units_per_arm(self):
        # Direct regression test for the denominator fix itself
        # (audit: 03-High): total_stages must be
        # len(stages) * units_this_round, not len(stages) alone —
        # otherwise a candidate that only covers 1/2 of this round's
        # assigned units clears coverage_min=0.7 on the UNSCALED
        # denominator (1/1 = 100%) when it should read 50% and get
        # dropped. Forces the mismatch directly: units_per_arm=(2,)
        # declares 2 units/round, but only 1 TaskUnit is actually
        # supplied, so every candidate's real attempt count is capped
        # at 1 regardless of provider behavior.
        graph = _graph_single_stage()
        storage = await _init(InMemoryStorage())
        schedule = HalvingSchedule(round_sizes=(2, 1), units_per_arm=(2, 2), pool_size=1)
        cfg = _cfg(
            candidates=["a", "b"], task_units=_units(1), stages=graph, storage=storage,
            schedule=schedule, provider_factory=lambda m: _RecordingProvider(model=m, call_log=[]),
        )
        result = await run_round(cfg)

        assert result.candidates_out == []
        for model in ("a", "b"):
            assert "below coverage_min" in result.eliminated_reasons[model]
            assert "1/2" in result.eliminated_reasons[model]


# ──────────────────────────────────────────────────────────────────────
# Parse-failure caching fix (Critical)
# ──────────────────────────────────────────────────────────────────────

class TestParseFailureCaching:
    async def test_parse_failure_sets_prefix_and_is_not_cached(self):
        def failing_parser(*, task_unit, response_text, input_tokens, output_tokens):
            return None  # simulate "couldn't parse this"

        graph = StageGraph([Stage(id="root", op="enrich", prompt_builder=_pb, parser=failing_parser)])
        storage = await _init(InMemoryStorage())
        call_log: list[str] = []
        provider = _RecordingProvider(model="m1", call_log=call_log, response_text="not valid json but long enough to pass length check")

        cfg1 = _cfg(candidates=["m1"], task_units=_units(1), stages=graph, storage=storage, provider_factory=lambda m: provider)
        await run_round(cfg1)

        rows = [r async for r in storage.query_rows(experiment_tag=TAG)]
        assert len(rows) == 1
        assert rows[0].error_class is None  # HTTP-level success
        assert rows[0].parse_failure_prefix is not None
        assert "not valid json" in rows[0].parse_failure_prefix

        # A second "round" against the SAME storage/tag must NOT get a
        # cache hit for this composite_hash — a parse failure must be
        # retried, not permanently cached as a success.
        call_log.clear()
        resume_cache = ResumeCache()
        await resume_cache.populate_from_storage(storage)
        assert len(resume_cache) == 0  # nothing usable got cached

        cfg2 = _cfg(candidates=["m1"], task_units=_units(1), stages=graph, storage=storage, provider_factory=lambda m: provider)
        cfg2.resume_cache = resume_cache
        await run_round(cfg2)
        assert call_log == ["m1"]  # retried for real, not served from cache

    async def test_raising_parser_treated_same_as_returning_none(self):
        # _parse_safely swallows a parser EXCEPTION and returns None,
        # same as a parser that just returns None on its own -- but
        # only the return-None case had a regression test above.
        # Confirms the raise path lands on the exact same
        # parse_failure_prefix / not-cached outcome (audit: 05-High).
        def raising_parser(*, task_unit, response_text, input_tokens, output_tokens):
            raise ValueError("malformed input, cannot parse")

        graph = StageGraph([Stage(id="root", op="enrich", prompt_builder=_pb, parser=raising_parser)])
        storage = await _init(InMemoryStorage())
        call_log: list[str] = []
        provider = _RecordingProvider(model="m1", call_log=call_log, response_text="not valid json but long enough to pass length check")

        cfg = _cfg(candidates=["m1"], task_units=_units(1), stages=graph, storage=storage, provider_factory=lambda m: provider)
        await run_round(cfg)

        rows = [r async for r in storage.query_rows(experiment_tag=TAG)]
        assert len(rows) == 1
        assert rows[0].error_class is None  # HTTP-level success
        assert rows[0].parse_failure_prefix is not None

        resume_cache = ResumeCache()
        await resume_cache.populate_from_storage(storage)
        assert len(resume_cache) == 0  # nothing usable got cached


# ──────────────────────────────────────────────────────────────────────
# PromptBuilder-raises failure (High)
# ──────────────────────────────────────────────────────────────────────

class TestPromptBuildFailure:
    async def test_raising_prompt_builder_creates_internal_pipeline_error_row(self):
        def failing_pb(*, task_unit, ctx, lang=None):
            raise ValueError("boom: this PromptBuilder always raises")

        graph = StageGraph([Stage(id="root", op="enrich", prompt_builder=failing_pb, parser=_parser)])
        storage = await _init(InMemoryStorage())
        call_log: list[str] = []
        cfg = _cfg(
            candidates=["m1"], task_units=_units(1), stages=graph, storage=storage,
            provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        await run_round(cfg)

        assert call_log == []  # the LLM was never called - the builder failed before that
        rows = [r async for r in storage.query_rows(experiment_tag=TAG)]
        assert len(rows) == 1
        assert rows[0].error_class == "InternalPipelineError"
        assert rows[0].response is None
        assert rows[0].cost_usd == 0.0

    async def test_raising_prompt_builder_does_not_break_coverage_gate(self):
        # Direct regression test for the "why it matters" scenario: a
        # PromptBuilder bug that only reproduces for ONE stage must not
        # silently shrink n_stages_attempted enough to trip the
        # coverage_min=0.7 default and eliminate an otherwise-healthy
        # candidate (audit: 02-High).
        call_log: list[str] = []

        def failing_pb(*, task_unit, ctx, lang=None):
            raise ValueError("boom")

        graph = StageGraph([
            Stage(id="s1", op="s1", prompt_builder=_pb, parser=_parser),
            Stage(id="s2", op="s2", prompt_builder=failing_pb, parser=_parser),
        ])
        storage = await _init(InMemoryStorage())
        schedule = HalvingSchedule(round_sizes=(2, 2), units_per_arm=(1, 1), pool_size=1)
        cfg = _cfg(
            candidates=["a", "b"], task_units=_units(1), stages=graph, storage=storage,
            schedule=schedule, provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        result = await run_round(cfg)

        assert set(result.candidates_out) == {"a", "b"}
        for reason in result.eliminated_reasons.values():
            assert "coverage_min" not in reason


# ──────────────────────────────────────────────────────────────────────
# 3+ DEAD-error circuit breaker
# ──────────────────────────────────────────────────────────────────────

class TestCircuitBreaker:
    async def test_aborts_pipeline_after_three_dead_errors(self):
        graph = StageGraph([Stage(id=f"s{i}", op=f"op{i}", prompt_builder=lambda i=i, **kw: ("s", f"u{i}"), parser=_parser) for i in range(5)])
        storage = await _init(InMemoryStorage())
        call_log: list[str] = []
        cfg = _cfg(
            candidates=["m1"], task_units=_units(1), stages=graph, storage=storage,
            provider_factory=lambda m: _AlwaysFailProvider(model=m, call_log=call_log),
        )
        await run_round(cfg)
        # 5 stages exist, but the pipeline must abort after the 3rd
        # DEAD-classified error — no more than 3 calls/rows.
        assert len(call_log) == 3
        rows = [r async for r in storage.query_rows(experiment_tag=TAG)]
        assert len(rows) == 3
        for row in rows:
            assert row.error_class == "ModelNotFound"


# ──────────────────────────────────────────────────────────────────────
# Quarantine (storm-detection) — StageContext.quarantined
# ──────────────────────────────────────────────────────────────────────

class TestQuarantine:
    async def test_cost_spike_quarantines_remaining_stages_for_that_pair(self):
        graph = StageGraph([
            Stage(id="root", op="expensive", budget_per_call=0.01, prompt_builder=_pb, parser=_parser),
            Stage(id="cheap", op="cheap", parent_stage="root", prompt_builder=_pb, parser=_parser),
        ])
        storage = await _init(InMemoryStorage())
        call_log: list[str] = []
        # last_cost_usd=1.0 vs budget_per_call=0.01 * quarantine_cost_multiplier
        # (default 10.0) = 0.1 threshold -> the FIRST stage's call already
        # trips quarantine, so "cheap" must never be called for this pair.
        cfg = _cfg(
            candidates=["m1"], task_units=_units(1), stages=graph, storage=storage,
            provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log, last_cost_usd=1.0, last_effective_cost_usd=1.0),
        )
        await run_round(cfg)

        assert call_log == ["m1"]  # only "root" ran; "cheap" was skipped
        rows = [r async for r in storage.query_rows(experiment_tag=TAG)]
        assert len(rows) == 1
        assert rows[0].stage == "root"

    async def test_duration_spike_quarantines_remaining_stages_for_that_pair(self):
        class _SlowProvider:
            def __init__(self, model):
                self.model = model
                self.last_input_tokens = 10
                self.last_output_tokens = 5
                self.last_reasoning_tokens = 0
                self.last_cost_usd = 0.001
                self.last_effective_cost_usd = 0.001

            async def generate(self, *, prompt, system):
                await asyncio.sleep(0.05)
                return '{"ok": true, "padding": "enough length here"}'

        graph = StageGraph([
            Stage(id="root", op="slow", prompt_builder=_pb, parser=_parser),
            Stage(id="fast", op="fast", parent_stage="root", prompt_builder=_pb, parser=_parser),
        ])
        storage = await _init(InMemoryStorage())
        cfg = _cfg(
            candidates=["m1"], task_units=_units(1), stages=graph, storage=storage,
            provider_factory=lambda m: _SlowProvider(m),
        )
        cfg.quarantine_duration_sec = 0.01  # tiny threshold so the 0.05s sleep trips it
        await run_round(cfg)

        rows = [r async for r in storage.query_rows(experiment_tag=TAG)]
        assert len(rows) == 1
        assert rows[0].stage == "root"

    async def test_quarantine_does_not_affect_other_pairs(self):
        # Quarantine is per-(model, task_unit) pipeline, not global —
        # a different candidate's pipeline must run unaffected.
        graph = StageGraph([
            Stage(id="root", op="expensive", budget_per_call=0.01, prompt_builder=_pb, parser=_parser),
            Stage(id="cheap", op="cheap", parent_stage="root", prompt_builder=_pb, parser=_parser),
        ])
        storage = await _init(InMemoryStorage())
        call_log: list[str] = []

        def factory(m):
            cost = 1.0 if m == "a" else 0.001
            return _RecordingProvider(model=m, call_log=call_log, last_cost_usd=cost, last_effective_cost_usd=cost)

        cfg = _cfg(
            candidates=["a", "b"], task_units=_units(1), stages=graph, storage=storage,
            provider_factory=factory,
        )
        await run_round(cfg)

        rows = [r async for r in storage.query_rows(experiment_tag=TAG)]
        rows_by_model = {(r.model, r.stage) for r in rows}
        assert ("a", "root") in rows_by_model
        assert ("a", "cheap") not in rows_by_model  # quarantined
        assert ("b", "root") in rows_by_model
        assert ("b", "cheap") in rows_by_model  # unaffected


# ──────────────────────────────────────────────────────────────────────
# Budget gate skip
# ──────────────────────────────────────────────────────────────────────

class TestBudgetGateSkip:
    async def test_zero_cap_skips_all_calls(self):
        graph = _graph_single_stage()
        storage = await _init(InMemoryStorage())
        call_log: list[str] = []
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=0.0)})
        cfg = _cfg(
            candidates=["m1"], task_units=_units(1), stages=graph, storage=storage,
            budget_gate=gate, provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        await run_round(cfg)
        assert call_log == []
        rows = [r async for r in storage.query_rows(experiment_tag=TAG)]
        assert rows == []

    async def test_headroom_cap_lets_call_through_and_records_cost(self):
        """A non-zero cap with real headroom must let the call proceed AND
        reconcile the reservation via ``budget_gate.record_cost`` (as
        opposed to the zero-cap case above, which skips before ever
        reaching that call)."""
        graph = _graph_single_stage()
        storage = await _init(InMemoryStorage())
        call_log: list[str] = []
        gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=100.0)})
        cfg = _cfg(
            candidates=["m1"], task_units=_units(1), stages=graph, storage=storage,
            budget_gate=gate, provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        await run_round(cfg)
        assert call_log == ["m1"]
        # `record_cost` releases the reservation `check()` made -- the
        # gate's internal ledger must show real spend, not stay at 0.
        assert gate._spent_by_key[(str(TAG), "enrich")] > 0.0


# ──────────────────────────────────────────────────────────────────────
# provider_label
# ──────────────────────────────────────────────────────────────────────

class TestProviderLabel:
    async def test_custom_provider_label_recorded_on_row(self):
        graph = _graph_single_stage()
        storage = await _init(InMemoryStorage())
        call_log: list[str] = []
        cfg = _cfg(
            candidates=["m1"], task_units=_units(1), stages=graph, storage=storage,
            provider_label="anthropic-direct",
            provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        await run_round(cfg)
        rows = [r async for r in storage.query_rows(experiment_tag=TAG)]
        assert len(rows) == 1
        assert rows[0].provider == "anthropic-direct"


# ──────────────────────────────────────────────────────────────────────
# Call-count regressions for perf fixes without an observable output
# difference (audit: 06-Low #8, #9)
# ──────────────────────────────────────────────────────────────────────

class TestCallCountRegressions:
    async def test_topo_order_computed_once_per_round_not_per_pipeline(self):
        graph = _graph_single_stage()
        call_count = {"n": 0}
        original_topo_order = graph.topo_order

        def _counting_topo_order():
            call_count["n"] += 1
            return original_topo_order()

        graph.topo_order = _counting_topo_order  # type: ignore[method-assign]

        storage = await _init(InMemoryStorage())
        cfg = _cfg(
            candidates=["a", "b"], task_units=_units(3), stages=graph, storage=storage,
            provider_factory=lambda m: _RecordingProvider(model=m, call_log=[]),
        )
        await run_round(cfg)
        # 2 candidates x 3 units = 6 pipelines, but topo_order() must be
        # computed exactly ONCE for the whole round (it's a pure
        # function of cfg.stages, identical for every pipeline), not
        # once per pipeline.
        assert call_count["n"] == 1

    async def test_upsert_prompts_deduped_within_a_round(self):
        # Every candidate in this test shares the SAME prompt_builder
        # output (a stage with no candidate-specific text), so all 3
        # candidates x 2 units = 6 pipelines hash to the identical
        # composite_hash. upsert_prompts must be called ONCE for that
        # hash per round (seen_prompt_hashes gate), not once per pipeline.
        def _uniform_pb(*, task_unit, ctx, lang=None):
            return ("sys", "identical user text for every pipeline")

        graph = StageGraph([Stage(id="root", op="enrich", prompt_builder=_uniform_pb, parser=_parser)])

        class _CountingStorage(InMemoryStorage):
            def __init__(self):
                super().__init__()
                self.upsert_calls = 0

            async def upsert_prompts(self, *, system_text, user_text):
                self.upsert_calls += 1
                return await super().upsert_prompts(system_text=system_text, user_text=user_text)

        storage = _CountingStorage()
        await storage.initialize()
        cfg = _cfg(
            candidates=["a", "b", "c"], task_units=_units(2), stages=graph, storage=storage,
            provider_factory=lambda m: _RecordingProvider(model=m, call_log=[]),
        )
        await run_round(cfg)
        assert storage.upsert_calls == 1

    async def test_winner_substrate_preload_issues_one_query_per_stage_not_per_pair(self):
        # load_winner_substrates_for_round must be O(stages) storage
        # queries per round, not O(candidates x task_units) -- the N+1
        # shape the audit warned a naive per-pair implementation would
        # reintroduce (a sibling single-pair helper, load_winner_substrate,
        # still exists for tests/ad-hoc callers with an explicit N+1
        # warning in its own docstring; the round driver must use the
        # batched one instead).
        def child_pb(*, task_unit, ctx, lang=None):
            return ("sys child", f"user parent={ctx.outputs.get('root')}")

        def child_parser(*, task_unit, response_text, input_tokens, output_tokens):
            return {"raw": response_text} if response_text else None

        graph = StageGraph([
            Stage(id="root", op="root", prompt_builder=_pb, parser=_parser),
            Stage(id="child", op="child", parent_stage="root", prompt_builder=child_pb, parser=child_parser),
        ])

        class _CountingStorage(InMemoryStorage):
            def __init__(self):
                super().__init__()
                self.query_rows_calls = 0

            async def query_rows(self, *, experiment_tag, stage=None):
                self.query_rows_calls += 1
                async for r in super().query_rows(experiment_tag=experiment_tag, stage=stage):
                    yield r

        storage = _CountingStorage()
        await storage.initialize()
        schedule = HalvingSchedule(round_sizes=(3, 3), units_per_arm=(2, 2), pool_size=1)
        halving = Halving(schedule=schedule)
        candidates = ["a", "b", "c"]

        cfg1 = _cfg(
            candidates=candidates, task_units=_units(2), stages=graph, storage=storage,
            schedule=schedule, halving=halving, round_idx=1,
            provider_factory=lambda m: _RecordingProvider(model=m, call_log=[]),
        )
        await run_round(cfg1)

        calls_before_round2 = storage.query_rows_calls
        cfg2 = _cfg(
            candidates=candidates, task_units=_units(2), stages=graph, storage=storage,
            schedule=schedule, halving=halving, round_idx=2,
            provider_factory=lambda m: _RecordingProvider(model=m, call_log=[]),
        )
        await run_round(cfg2)
        preload_calls = storage.query_rows_calls - calls_before_round2

        # _preload_winner_substrates issues 1 query_rows call per stage
        # that HAS a winner to preload (here: just "root", since "child"
        # has no children of its own to seed) -- NOT one per
        # (candidate, task_unit) pair, which would be 3*2=6 for round 2's
        # pool. The exact count is an implementation detail (compute_ranking
        # + select_per_stage_winners also call query_rows), so assert the
        # ORDER OF MAGNITUDE bound the N+1 fix guarantees, not an exact number.
        assert preload_calls < len(candidates) * 2


# ──────────────────────────────────────────────────────────────────────
# per_op_concurrency
# ──────────────────────────────────────────────────────────────────────

class TestPerOpConcurrency:
    async def test_per_op_cap_serialises_calls_for_that_op(self):
        in_flight = {"current": 0, "max": 0}
        lock = asyncio.Lock()

        class _SlowProvider:
            def __init__(self, model):
                self.model = model
                self.last_input_tokens = 1
                self.last_output_tokens = 1
                self.last_reasoning_tokens = 0
                self.last_cost_usd = 0.001
                self.last_effective_cost_usd = 0.001

            async def generate(self, *, prompt, system):
                async with lock:
                    in_flight["current"] += 1
                    in_flight["max"] = max(in_flight["max"], in_flight["current"])
                await asyncio.sleep(0.02)
                async with lock:
                    in_flight["current"] -= 1
                return '{"ok": true, "padding": "enough length here too"}'

        graph = _graph_single_stage(op="enrich")
        storage = await _init(InMemoryStorage())
        cfg = _cfg(
            candidates=["a", "b", "c", "d"], task_units=_units(1), stages=graph, storage=storage,
            per_op_concurrency={"enrich": 1}, provider_factory=_SlowProvider,
        )
        await run_round(cfg)
        assert in_flight["max"] == 1


# ──────────────────────────────────────────────────────────────────────
# Infra-failure surfacing (RoundResult.notes)
# ──────────────────────────────────────────────────────────────────────

class TestInfraFailureSurfacing:
    async def test_storage_write_failure_surfaces_in_notes_and_still_counts_attempted(self):
        class _FlakyStorage(InMemoryStorage):
            async def record_call(self, row: RunRow) -> str:
                raise ConnectionError("storage backend unreachable")

        storage = _FlakyStorage()
        await storage.initialize()
        graph = _graph_single_stage()
        call_log: list[str] = []
        cfg = _cfg(
            candidates=["m1"], task_units=_units(1), stages=graph, storage=storage,
            provider_factory=lambda m: _RecordingProvider(model=m, call_log=call_log),
        )
        result = await run_round(cfg)
        # The paid call happened (call_log non-empty) even though
        # persistence failed — this must be visible in notes, not
        # silently swallowed.
        assert call_log == ["m1"]
        assert any("infrastructure failure" in note for note in result.notes)


# ──────────────────────────────────────────────────────────────────────
# error_message secret redaction (audit: security 07-Low)
# ──────────────────────────────────────────────────────────────────────

class TestErrorMessageRedaction:
    async def test_provider_exception_with_leaked_secret_is_scrubbed_before_persisting(self):
        class _LeakySecretProvider:
            def __init__(self, model):
                self.model = model

            async def generate(self, *, prompt, system):
                raise RuntimeError("upstream rejected request: Authorization: Bearer sk-liveSECRETTOKEN1234567890")

        graph = _graph_single_stage()
        storage = await _init(InMemoryStorage())
        cfg = _cfg(
            candidates=["m1"], task_units=_units(1), stages=graph, storage=storage,
            provider_factory=lambda m: _LeakySecretProvider(m),
        )
        await run_round(cfg)

        rows = [r async for r in storage.query_rows(experiment_tag=TAG)]
        assert len(rows) == 1
        assert rows[0].error_message is not None
        assert "sk-liveSECRETTOKEN1234567890" not in rows[0].error_message
        assert "Bearer ***" in rows[0].error_message


# ──────────────────────────────────────────────────────────────────────
# _build_prompt / _parse_safely direct exercises (branch-coverage ratchet)
# ──────────────────────────────────────────────────────────────────────

class TestBuildPromptDirect:
    async def test_async_prompt_builder_is_awaited(self):
        """A PromptBuilder MAY be a coroutine function -- ``_build_prompt``
        awaits it when ``asyncio.iscoroutine(result)`` is True."""

        async def async_pb(*, task_unit, ctx, lang=None):
            return (f"sys {task_unit.id}", "user")

        stage = Stage(id="root", op="enrich", prompt_builder=async_pb, parser=_parser)
        ctx = StageContext(task_unit=TaskUnit(id="u0", stratum="v"))
        sys_p, usr_p = await _build_prompt(stage, ctx.task_unit, ctx)
        assert sys_p == "sys u0"
        assert usr_p == "user"

    async def test_malformed_prompt_builder_return_raises(self):
        """A PromptBuilder returning anything other than a 2-tuple is a
        contract violation and must raise, not silently misbehave."""

        def bad_pb(*, task_unit, ctx, lang=None):
            return "not a tuple"

        stage = Stage(id="root", op="enrich", prompt_builder=bad_pb, parser=_parser)
        ctx = StageContext(task_unit=TaskUnit(id="u0", stratum="v"))
        with pytest.raises(ValueError, match="must return"):
            await _build_prompt(stage, ctx.task_unit, ctx)


class TestParseSafelyDirect:
    def test_none_response_text_short_circuits_to_none(self):
        """A None response_text (e.g. a winner-substrate row whose own
        call failed) must short-circuit without ever calling the
        stage's parser -- there is nothing to parse."""
        calls: list[str] = []

        def spy_parser(*, task_unit, response_text, input_tokens, output_tokens):
            calls.append("called")
            return {"raw": response_text}

        stage = Stage(id="root", op="enrich", prompt_builder=_pb, parser=spy_parser)
        task_unit = TaskUnit(id="u0", stratum="v")
        result = _parse_safely(stage, task_unit, None, 0, 0)
        assert result is None
        assert calls == []
