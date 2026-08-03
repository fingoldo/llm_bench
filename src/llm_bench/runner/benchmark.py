"""Benchmark — top-level facade composing pool + stages + storage + halving.

A consumer instantiates ``Benchmark`` once with all its plug-ins and then
calls ``run_phase(tag=..., rounds=[1,2,3,4])`` to execute the full Sequential
Halving cascade end-to-end.

The facade is thin on purpose: it owns lifecycle (storage init / close,
resume cache pre-load) and delegates per-round work to ``run_round`` from
``round_runner``. Adding new orchestration features (pre-flight credit
check, post-round confirmation gate) belongs HERE, not on the user-facing
``Stage`` API.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, cast

from llm_bench.core.types import ExperimentTag, StageGraph, TaskUnit
from llm_bench.cost.filter import CostFilter
from llm_bench.halving import Halving, HalvingSchedule
from llm_bench.pool.base import TaskPool
from llm_bench.ranking import RankingReport, RowScorer, compute_ranking
from llm_bench.runner.budget import BudgetGate
from llm_bench.runner.classify import classify_provider_error
from llm_bench.runner.resume import ResumeCache
from llm_bench.runner.round_runner import RoundConfig, run_round
from llm_bench.stage.base import GoldChecker
from llm_bench.storage.base import BenchmarkStorage

logger = logging.getLogger(__name__)


@dataclass
class PreflightResult:
    """Result of a single pre-round ping for one candidate model.

    ``ok=False`` means the model failed the ping for some reason (provider
    routing 404, rate limit, timeout, ...). ``error_class`` carries the
    canonical label from ``classify_provider_error`` so callers can act on
    a small enum instead of free-form messages.
    """
    model: str
    ok: bool
    error_class: str | None = None
    error_message: str | None = None
    latency_sec: float = 0.0


@dataclass
class PhaseReport:
    """Aggregated result of a multi-round ``run_phase`` call."""
    experiment_tag: str
    rounds: list[Any] = field(default_factory=list)
    """List of ``RoundResult`` (one per round executed)."""
    final_candidates: list[str] = field(default_factory=list)
    final_ranking: RankingReport | None = None
    elapsed_sec: float = 0.0
    preflight: dict[str, PreflightResult] = field(default_factory=dict)
    """Per-model preflight verdict when ``run_phase(preflight=True)``. Empty
    when preflight was skipped."""


@dataclass
class Benchmark:
    """Top-level benchmark orchestrator.

    Wiring:
      - ``task_pool``: source of TaskUnits (VocabApp synsets, JobApp jobs, …)
      - ``stages``: pipeline DAG (each TaskUnit traverses this graph for
        every candidate)
      - ``storage``: pluggable persistence (Postgres / File / InMemory)
      - ``halving_schedule``: round_sizes + units_per_arm
      - ``cost_filter`` / ``gold_checker`` / ``row_scorer``: optional plug-ins
      - ``budget_gate``: optional per-op spend cap
      - ``provider_factory``: defaults to ``pyutilz.llm.get_llm_provider``;
        override for tests with a fake provider

    Lifecycle:
      ``run_phase`` calls ``storage.initialize()`` and pre-loads the
      ``ResumeCache``. The caller must invoke ``await bench.aclose()`` to
      release storage resources, OR use ``async with Benchmark(...) as b``.
    """
    task_pool: TaskPool
    stages: StageGraph
    storage: BenchmarkStorage
    halving_schedule: HalvingSchedule = field(default_factory=HalvingSchedule)
    cost_filter: CostFilter | None = None
    gold_checker: GoldChecker | None = None
    row_scorer: RowScorer | None = None
    budget_gate: BudgetGate | None = None
    provider_factory: Any = None
    thinking: str = ""
    provider_label: str = "openrouter"
    global_concurrency: int = 30
    per_op_concurrency: dict[str, int] = field(default_factory=dict)

    _resume_cache: ResumeCache = field(default_factory=ResumeCache, init=False)
    _initialized: bool = field(default=False, init=False)

    async def initialize(self) -> None:
        """One-shot setup: storage schema + resume cache pre-load."""
        if self._initialized:
            return
        await self.storage.initialize()
        await self._resume_cache.populate_from_storage(self.storage)
        self._initialized = True

    async def aclose(self) -> None:
        """Release storage resources."""
        await self.storage.close()

    async def __aenter__(self) -> "Benchmark":
        await self.initialize()
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        await self.aclose()

    async def preflight(
        self,
        candidates: list[str],
        *,
        prompt: str = "Reply with one word.",
        system: str = "You are a benchmark preflight ping.",
        max_tokens: int = 5,
        timeout_sec: float = 30.0,
        concurrency: int = 8,
    ) -> dict[str, PreflightResult]:
        """Tiny ping-call per candidate to detect dead/misrouted models.

        Catches the failure modes that silently waste a benchmark run:
          - vendor pin pointing at an upstream that doesn't host the model
            (consumer wires ``provider_order=("google",)`` for ``gemma-*``
            and OR returns 404 because gemma is third-party-hosted)
          - model removed from the OpenRouter catalogue between discovery
            and run_phase (catalogue cache lag)
          - 405 ``Method not allowed`` on a model whose endpoint is
            mis-configured in OR's routing layer
          - genuinely-dead models (no available endpoints across providers)

        Returns ``{model: PreflightResult}``. Caller filters
        ``[m for m, r in results.items() if r.ok]`` and passes the survivors
        as the ``candidates`` list to ``run_phase``.

        Cost: 1 small call per candidate. With max_tokens=5 and a one-line
        prompt, ~$0.001 per model on cheap routes; ~$0.005 on expensive.
        """
        if self.provider_factory is None:
            raise ValueError("preflight() requires provider_factory to be set on the Benchmark")
        sem = asyncio.Semaphore(concurrency)

        async def _one(model: str) -> PreflightResult:
            async with sem:
                t0 = time.monotonic()
                try:
                    provider = self.provider_factory(model)
                except Exception as e:
                    return PreflightResult(
                        model=model, ok=False,
                        error_class="ProviderFactoryError",
                        error_message=str(e)[:200],
                        latency_sec=time.monotonic() - t0,
                    )
                try:
                    coro = provider.generate(
                        prompt=prompt, system=system, max_tokens=max_tokens,
                    )
                    await asyncio.wait_for(coro, timeout=timeout_sec)
                    return PreflightResult(
                        model=model, ok=True,
                        latency_sec=time.monotonic() - t0,
                    )
                except TimeoutError:
                    return PreflightResult(
                        model=model, ok=False,
                        error_class="LLMCallTimeout",
                        error_message=f"preflight exceeded {timeout_sec}s",
                        latency_sec=time.monotonic() - t0,
                    )
                except Exception as e:
                    msg = str(e)
                    return PreflightResult(
                        model=model, ok=False,
                        error_class=classify_provider_error(type(e).__name__, msg),
                        error_message=msg[:200],
                        latency_sec=time.monotonic() - t0,
                    )

        results = await asyncio.gather(*(_one(m) for m in candidates))
        verdicts = {r.model: r for r in results}
        n_ok = sum(1 for r in results if r.ok)
        n_dead = len(results) - n_ok
        if n_dead:
            sample = [f"{r.model}: {r.error_class}" for r in results if not r.ok][:5]
            logger.warning(
                "[preflight] %d/%d candidates failed ping. First %d: %s",
                n_dead, len(candidates), len(sample), sample,
            )
        else:
            logger.info("[preflight] all %d candidates ok", len(candidates))
        return verdicts

    async def run_phase(
        self,
        *,
        tag: str | ExperimentTag,
        candidates: list[str],
        rounds: list[int] | None = None,
        units: list[TaskUnit] | None = None,
        preflight: bool = False,
    ) -> PhaseReport:
        """Execute the Sequential Halving cascade across the listed rounds.

        Args:
            tag: experiment label persisted on every row.
            candidates: starting model pool (round 1 input). Survivors
                propagate via ``RoundResult.candidates_out``.
            rounds: 1-indexed round indices to execute. Defaults to
                ``range(1, len(schedule.round_sizes) + 1)``.
            units: pre-fetched TaskUnit list. When omitted, the benchmark
                samples from ``task_pool`` using ``schedule.pool_size``.
            preflight: when True, ping every candidate before round 1 and
                drop the dead ones. Adds ~30s + ~$0.10 (per 100 candidates)
                in exchange for filtering out misrouted models that would
                otherwise burn the per-stage budget on retries that can't
                succeed. The verdict map lands in ``PhaseReport.preflight``.

        Returns:
            ``PhaseReport`` with per-round results + final ranking.
        """
        if not self._initialized:
            await self.initialize()

        exp_tag = ExperimentTag(str(tag))
        rounds = rounds or list(range(1, len(self.halving_schedule.round_sizes) + 1))
        units = units or await self._sample_units()
        halving = Halving(schedule=self.halving_schedule)

        report = PhaseReport(experiment_tag=str(exp_tag))
        t0 = time.monotonic()
        current_candidates = list(candidates)

        if preflight:
            verdicts = await self.preflight(current_candidates)
            report.preflight = verdicts
            survivors = [m for m, r in verdicts.items() if r.ok]
            dropped = len(current_candidates) - len(survivors)
            if dropped:
                logger.info(
                    "[run_phase] preflight dropped %d/%d misrouted/dead " "candidates before round 1",
                    dropped,
                    len(current_candidates),
                )
            current_candidates = survivors

        for round_idx in rounds:
            if not current_candidates:
                # `break` right below means this can fire at most once per
                # run_phase() call — not a real hot-loop spam risk, but keep
                # it at INFO (not WARNING) since "cascade already ended" is
                # expected, not exceptional.
                logger.info("[run_phase] round %d: no candidates left, " "halting cascade", round_idx)
                break
            logger.info("[run_phase] round %d/%d - %d candidates, %d task units", round_idx, rounds[-1], len(current_candidates), len(units))
            cfg = RoundConfig(
                experiment_tag=exp_tag,
                round_idx=round_idx,
                candidates=current_candidates,
                task_units=units,
                stages=self.stages,
                schedule=self.halving_schedule,
                storage=self.storage,
                halving=halving,
                resume_cache=self._resume_cache,
                budget_gate=self.budget_gate,
                gold_checker=self.gold_checker,
                row_scorer=self.row_scorer,
                global_concurrency=self.global_concurrency,
                per_op_concurrency=self.per_op_concurrency,
                provider_factory=self.provider_factory,
                thinking=self.thinking,
                provider_label=self.provider_label,
            )
            round_result = await run_round(cfg)
            report.rounds.append(round_result)
            current_candidates = round_result.candidates_out
            logger.info("[run_phase] round %d -> %d survivors", round_idx, len(current_candidates))

        report.final_candidates = current_candidates
        report.final_ranking = await compute_ranking(
            self.storage, exp_tag,
            row_scorer=self.row_scorer,
            gold_checker=self.gold_checker,
            task_unit_lookup=lambda uid: next(
                (u for u in units if u.id == uid), None,
            ),
        )
        report.elapsed_sec = time.monotonic() - t0
        return report

    async def _sample_units(self) -> list[TaskUnit]:
        """Pull the working pool from ``task_pool`` once at phase start.

        ``TaskPool.sample()``/``total_size()`` are documented (Protocol
        docstring in ``pool/base.py``) as "MAY be async-aware (a
        coroutine method works too — the runner awaits any awaitable
        result)" — every OTHER Protocol-typed callback in this
        framework (``PromptBuilder``, ``RowScorer``) honours that same
        convention, but this method used to call ``self.task_pool.sample(n)``
        synchronously and use the result directly. A `TaskPool` written
        exactly to the documented contract (``async def sample`` — a
        natural choice for a pool backed by an external API/DB) returned
        a live coroutine object here, and `len(units)` below crashed
        with `TypeError: object of type 'coroutine' has no len()` on
        the very first call — a guaranteed crash for anyone who followed
        the Protocol docstring literally (audit: 04-High).
        """
        n = self.halving_schedule.pool_size
        sample_result = self.task_pool.sample(n)
        units = await sample_result if asyncio.iscoroutine(sample_result) else cast("list[TaskUnit]", sample_result)
        total_result = self.task_pool.total_size()
        total = await total_result if asyncio.iscoroutine(total_result) else cast(int, total_result)
        logger.info("[run_phase] sampled %d/%d task units", len(units), total)
        return units
