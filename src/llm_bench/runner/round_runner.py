"""Single-round driver — orchestrates one Halving round end-to-end.

Per round:
  1. Discover candidates (or use a pre-supplied list).
  2. Assign task units per candidate (assign_task_units_for_round).
  3. Per (model, task_unit) pair: run the StageGraph in topo order.
     Each stage call goes through the resume cache first; on miss,
     the LLM is invoked via ``pyutilz.llm.get_llm_provider``. Result
     row is persisted to storage. Stages with a ``parent_stage`` read
     that parent's substrate from the PREVIOUS round's per-stage
     winner (falling back to this candidate's own output when no
     winner substrate exists yet — first round / new task unit).
     Stages marked ``is_validator=True`` are answered by a DIFFERENT
     candidate (Layer-1 cross-family independence — a model never
     validates its own output) while the resulting row is still
     credited to the producer being judged.
  4. After every pair completes: compute_ranking → select_per_stage_winners
     → halving.promote → optionally persist winners.

Concurrency: ``asyncio.gather`` over (model, task_unit) pairs with a
global semaphore. Per-stage caps are honoured by gating each stage call
inside the pipeline behind a per-op semaphore.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

from llm_bench.core.hashing import hash_text, prompt_hashes
from llm_bench.core.redaction import redact_secrets
from llm_bench.core.types import (
    CachedResponse,
    ExperimentTag,
    RunRow,
    Stage,
    StageGraph,
    TaskUnit,
)
from llm_bench.halving import (
    DEAD_ERROR_CLASSES,
    Halving,
    HalvingSchedule,
    RoundResult,
    assign_task_units_for_round,
    select_validator_for_producer,
)
from llm_bench.ranking import (
    RankingReport,
    RowScorer,
    StageRanking,
    compute_ranking,
    load_winner_substrates_for_round,
    select_per_stage_winners,
)
from llm_bench.runner.budget import BudgetGate
from llm_bench.runner.classify import classify_provider_error
from llm_bench.runner.resume import ResumeCache
from llm_bench.stage.base import GoldChecker, StageContext
from llm_bench.storage.base import BenchmarkStorage

logger = logging.getLogger(__name__)

# Logged once per (round, reason) instead of once per pipeline so a
# large N=2 candidate pool doesn't spam identical warnings.
_validator_skip_logged: set[tuple[int, str]] = set()

# Per-key hit counters for _log_throttled — caps repeated identical
# warnings/exceptions from a single (model, task_unit) pipeline loop.
_throttle_hit_counts: dict[str, int] = defaultdict(int)
_LOG_THROTTLE_MAX = 3


def _log_throttled(key: str, level: int, msg: str, *args: Any, exc_info: bool = False) -> None:
    """Log at ``level`` for the first ``_LOG_THROTTLE_MAX`` hits of ``key``,
    then fall back to DEBUG — a pipeline that keeps failing the same way
    across many stages/task_units must not spam an identical WARNING/
    exception line once per hit."""
    _throttle_hit_counts[key] += 1
    if _throttle_hit_counts[key] <= _LOG_THROTTLE_MAX:
        logger.log(level, msg, *args, exc_info=exc_info)
    else:
        logger.debug(msg, *args, exc_info=exc_info)


@dataclass
class RoundConfig:
    """Knobs for a single round."""
    experiment_tag: ExperimentTag
    round_idx: int
    candidates: list[str]
    task_units: list[TaskUnit]
    stages: StageGraph
    schedule: HalvingSchedule
    storage: BenchmarkStorage
    halving: Halving
    resume_cache: ResumeCache
    budget_gate: BudgetGate | None = None
    gold_checker: GoldChecker | None = None
    row_scorer: RowScorer | None = None
    global_concurrency: int = 30
    """Max concurrent (model, task_unit) pipelines. When paired with
    ``PostgresStorage``, keep its ``max_connections`` in the same
    ballpark as this number (default 8 vs this default 30) — every
    pipeline makes at least 2 DB round-trips per stage
    (``upsert_prompts`` + ``record_call``), so a pool much smaller
    than ``global_concurrency`` serializes most of a round's I/O on
    connection acquisition, undercutting the throughput this setting
    implies (audit: 08-Medium). Not auto-reconciled — this is
    documentation, not a validated invariant, since a shared Postgres
    instance may have its own reasons to cap connections below what a
    single benchmark run would otherwise want."""
    per_op_concurrency: dict[str, int] = field(default_factory=dict)
    """``{op: max_concurrent}`` — per-op cap layered on top of the
    global semaphore. Defaults to no per-op cap."""
    provider_factory: Any = None
    """Callable ``(model_id) -> LLMProvider`` (any object with an
    async ``generate(prompt=..., system=...) -> str`` method). Defaults
    to ``pyutilz.llm.get_llm_provider("openrouter", model=...)``."""
    thinking: str = ""
    provider_label: str = "openrouter"
    """Identity recorded as ``RunRow.provider`` / used as the resume-
    cache key's provider component. Previously hardcoded to the literal
    string ``"openrouter"`` at every call site regardless of what
    ``provider_factory`` actually invoked — so a consumer overriding
    ``provider_factory`` with a non-OpenRouter backend got rows that
    silently misreported their own provenance, and, if it ever reused a
    model id that also existed on OpenRouter with byte-identical prompt
    text, a resume-cache collision between two genuinely different
    backends (audit: 01/03-High). Set this alongside a custom
    ``provider_factory`` to keep the two in sync.
    """
    quarantine_duration_sec: float = 300.0
    quarantine_cost_multiplier: float = 10.0
    """Storm-detection thresholds backing ``StageContext.quarantined``
    (docstring promise in ``stage/base.py`` that was previously never
    implemented — audit: 04/09-Medium). A single stage call whose
    duration exceeds ``quarantine_duration_sec``, OR whose cost exceeds
    ``stage.budget_per_call * quarantine_cost_multiplier``, marks the
    REST of that one (model, task_unit) pipeline as quarantined and
    stops it — a runaway reasoning chain on one stage doesn't keep
    spending on that same pair's remaining stages. Does not affect
    other (model, task_unit) pairs running concurrently.
    """


async def _preload_winner_substrates(cfg: RoundConfig) -> dict[tuple[str, str], RunRow]:
    """ONE batched query per parent-stage for the whole round (not one
    query per candidate/task_unit — see
    ``load_winner_substrates_for_round``'s docstring on why that would
    be an N+1) instead of the M_X mechanism being entirely unwired.
    Round 1 has no prior round, so this always returns empty then —
    every lookup falls back to each candidate's own output."""
    parent_stage_ids = {s.parent_stage for s in cfg.stages.stages if s.parent_stage}
    if cfg.round_idx <= 1 or not parent_stage_ids:
        return {}
    prev_winners = await cfg.storage.load_winners(
        experiment_tag=cfg.experiment_tag, round_idx=cfg.round_idx - 1,
    )
    if prev_winners is None:
        return {}
    return await load_winner_substrates_for_round(
        cfg.storage, winners=prev_winners,
        stages=parent_stage_ids, experiment_tag=cfg.experiment_tag,
    )


def _aggregate_stage_metric(
    report: RankingReport, extractor: "Callable[[StageRanking], dict[str, float]]",
) -> dict[str, float]:
    """Sum ``extractor(stage_ranking)`` across all stages per model,
    then average by stage count. Shared shape behind
    aggregate_scores/aggregate_eff_cost/aggregate_latency below — one
    implementation instead of copy-pasting the same sum-then-divide
    loop three times."""
    total: dict[str, float] = {}
    for sr in report.stages.values():
        for m, v in extractor(sr).items():
            total[m] = total.get(m, 0.0) + v
    if report.stages:
        for m in total:
            total[m] /= len(report.stages)
    return total


def _apply_last_round_score_bonus(aggregate_scores: dict[str, float], halving: Halving) -> None:
    """Mixes in the multi-specialty bonus EARNED LAST ROUND (the round
    that just finished, "this round", doesn't have its own bonus yet —
    it gets computed at the end of THIS promote() call, for the round
    after). This is what makes the bonus reach a real promotion
    decision instead of sitting unread in the RoundResult promote()
    returned last time (see Halving.last_score_bonus). Iterates the
    bonus dict, not aggregate_scores itself, so this isn't a mutate-
    while-iterate pattern on the same collection."""
    for m, bonus in halving.last_score_bonus.items():
        if bonus and m in aggregate_scores:
            aggregate_scores[m] = min(1.0, aggregate_scores[m] + bonus)


def _build_per_task_unit_scores(report: RankingReport) -> dict[str, list[float]]:
    """Per-task-unit score breakdown, averaged ACROSS stages per
    (model, task_unit) — activates mad_bootstrap_prune's paired-
    bootstrap variance/cost penalty instead of the plain aggregate
    fallback (audit: 02-High — the penalty existed but nothing fed it
    data). Pre-gold-blend (see StageRanking.model_task_unit_scores'
    docstring for why)."""
    per_task_unit_scores: dict[str, list[float]] = {}
    models = {m for sr in report.stages.values() for m in sr.model_task_unit_scores}
    for m in models:
        unit_scores: dict[str, list[float]] = {}
        for sr in report.stages.values():
            for tu, score in sr.model_task_unit_scores.get(m, {}).items():
                unit_scores.setdefault(tu, []).append(score)
        if unit_scores:
            per_task_unit_scores[m] = [sum(vals) / len(vals) for vals in unit_scores.values()]
    return per_task_unit_scores


def _attach_infra_error_notes(round_result: RoundResult, infra_errors: list[str], round_idx: int) -> None:
    """Surfaces _run_pipeline's persistence failures (storage/resume-
    cache/budget-gate write errors AFTER a paid LLM call) into
    RoundResult.notes so a caller reading the RETURN VALUE (not just
    logs) can detect that this happened, per the project's own
    collect-and-report convention elsewhere in this module."""
    if not infra_errors:
        return
    round_result.notes.append(
        f"{len(infra_errors)} infrastructure failure(s) persisting an "
        f"already-attempted call this round (storage/resume-cache/"
        f"budget-gate write errors AFTER the LLM call itself completed) "
        f"- results for those specific calls may be lost even though "
        f"n_stages_attempted still counted them. See logs for the "
        f"affected (stage, model, task_unit) triples.",
    )
    logger.warning(
        "[round %d] %d infrastructure failure(s) this round - see " "RoundResult.notes and preceding ERROR-level logs",
        round_idx, len(infra_errors),
    )


async def run_round(cfg: RoundConfig) -> RoundResult:
    """Execute one Halving round, persist results + winners.

    Returns the RoundResult from ``halving.promote(...)``. The next
    round's caller reads ``result.candidates_out`` for its candidate
    pool.
    """
    # Slice the task-unit pool for this round.
    units_per_candidate = assign_task_units_for_round(
        cfg.candidates,
        [u.id for u in cfg.task_units],
        cfg.round_idx,
        cfg.schedule,
    )
    units_by_id = {u.id: u for u in cfg.task_units}
    pairs = [(model, units_by_id[uid]) for model in cfg.candidates for uid in units_per_candidate[model] if uid in units_by_id]
    logger.info(
        "[round %d] %d pairs (candidates=%d * units/arm=%d)",
        cfg.round_idx, len(pairs), len(cfg.candidates),
        cfg.schedule.units_per_arm[cfg.round_idx - 1],
    )

    winner_substrates = await _preload_winner_substrates(cfg)

    # Topo order is a pure function of cfg.stages, identical for every
    # pipeline in this round — computed once here instead of once per
    # (model, task_unit) pipeline (audit: 06-Low).
    topo_order_list = cfg.stages.topo_order()

    # Concurrency machinery.
    global_sem = asyncio.Semaphore(cfg.global_concurrency)
    per_op_sems: dict[str, asyncio.Semaphore] = {op: asyncio.Semaphore(n) for op, n in cfg.per_op_concurrency.items()}
    n_stages_attempted: dict[str, int] = {}
    seen_prompt_hashes: set[str] = set()
    infra_errors: list[str] = []
    """Populated by _run_pipeline's persistence try/except — see
    _attach_infra_error_notes below for how this reaches the caller."""

    async def _one_pair(model: str, task_unit: TaskUnit) -> None:
        try:
            await _run_pipeline(
                cfg=cfg, model=model, task_unit=task_unit,
                global_sem=global_sem, per_op_sems=per_op_sems,
                n_stages_attempted=n_stages_attempted,
                winner_substrates=winner_substrates,
                topo_order_list=topo_order_list,
                seen_prompt_hashes=seen_prompt_hashes,
                infra_errors=infra_errors,
            )
        except Exception:
            # Safety net for anything that escapes the narrower
            # try/excepts inside _run_pipeline (prompt-build, parse,
            # and infra-persistence failures each have their own
            # handling now — see there for why those don't land here
            # instead). Anything reaching this one is a genuine bug in
            # framework/plugin code outside those known failure points.
            logger.exception("[round %d] pipeline failed for %s/%s", cfg.round_idx, model, task_unit.id)

    await asyncio.gather(*(_one_pair(m, u) for m, u in pairs))

    # Rank + promote.
    report = await compute_ranking(
        cfg.storage,
        cfg.experiment_tag,
        row_scorer=cfg.row_scorer,
        gold_checker=cfg.gold_checker,
        task_unit_lookup=lambda uid: units_by_id.get(uid),
    )
    aggregate_scores = _aggregate_stage_metric(report, lambda sr: sr.model_scores)
    _apply_last_round_score_bonus(aggregate_scores, cfg.halving)
    aggregate_eff_cost = _aggregate_stage_metric(report, lambda sr: sr.model_eff_cost)
    # Latency aggregate — mirrors aggregate_eff_cost's shape so
    # cost_tiebreak_key's throughput-aware tiebreak (already used when
    # ranker.py picks a single stage's winner) ALSO applies at the
    # halving-cut decision (which candidates survive to the next
    # round) — previously only the former got it (audit: 02-High).
    aggregate_latency = _aggregate_stage_metric(report, lambda sr: sr.model_avg_latency_sec)
    per_task_unit_scores = _build_per_task_unit_scores(report)

    per_stage_winners = {stage: sr.winner for stage, sr in report.stages.items() if sr.winner}
    # Coverage-gate denominator scaled by this round's units_per_arm:
    # n_stages_attempted sums attempts across ALL task units assigned
    # this round, so the denominator must be stage-count * units/arm to
    # match, not stage-count alone (which only requires ~1/units_per_arm
    # of the real maximum attempts to clear the coverage_min ratio,
    # catching only a total dropout rather than the partial-coverage
    # cases the gate was meant for — audit: 03-High).
    units_this_round = cfg.schedule.units_per_arm[cfg.round_idx - 1]
    round_result = cfg.halving.promote(
        cfg.round_idx,
        aggregate_scores,
        aggregate_eff_cost,
        per_stage_winners=per_stage_winners,
        n_stages_attempted=n_stages_attempted,
        total_stages=len(cfg.stages.stages) * units_this_round,
        mean_latency_sec=aggregate_latency or None,
        per_task_unit_scores=per_task_unit_scores or None,
    )
    # Persist winners for the next round to consume.
    ws = select_per_stage_winners(
        report,
        experiment_tag=cfg.experiment_tag,
        round_idx=cfg.round_idx,
        candidates_out=round_result.candidates_out,
    )
    await cfg.storage.persist_winners(
        experiment_tag=cfg.experiment_tag,
        round_idx=cfg.round_idx,
        winners=ws,
    )
    _attach_infra_error_notes(round_result, infra_errors, cfg.round_idx)
    return round_result


def _apply_winner_substrate(
    cfg: RoundConfig, stage: Stage, task_unit: TaskUnit, ctx: StageContext,
    winner_substrates: dict[tuple[str, str], RunRow],
) -> None:
    """M_X winner-substrate: for a stage with a parent, prefer the
    PREVIOUS round's per-stage winner's parsed output over this
    candidate's own — decouples "is this candidate good at stage k+1"
    from "did this same candidate also nail stage k", exactly the
    guarantee ranking/per_stage_winners.py's own docstring describes
    and that was never actually wired in (audit: Critical, the
    framework's own headline feature). Falls back to the candidate's
    own output (already populated by this pipeline's own earlier
    iteration, left untouched) when no substrate exists yet — first
    round, or a task unit the winner has no row for.
    """
    if stage.parent_stage is None:
        return
    substrate_row = winner_substrates.get((stage.parent_stage, task_unit.id))
    if substrate_row is None:
        return
    parent_stage_obj = cfg.stages.by_id(stage.parent_stage)
    ctx.outputs[stage.parent_stage] = _parse_safely(
        parent_stage_obj, task_unit, substrate_row.response,
        substrate_row.input_tokens or 0, substrate_row.output_tokens or 0,
    )
    ctx.parent_prompt_hashes[stage.parent_stage] = substrate_row.composite_hash
    ctx.response_texts[stage.parent_stage] = substrate_row.response or ""


def _resolve_validator_call_model(
    cfg: RoundConfig, stage: Stage, model: str, n_stages_attempted: dict[str, int],
) -> str | None:
    """Validator independence (Layer-1 HARD): a validator stage is
    answered by a DIFFERENT candidate than the one being judged — "a
    model never validates its own output" (core/types.py's own
    docstring for Stage.is_validator, previously never enforced —
    audit: Critical). The row is still credited to `model` (the
    producer) by the caller — ranking/halving elimination is about the
    PRODUCER's quality as judged by an independent model, not about the
    validator's own skill; only the ACTUAL LLM call uses the model this
    returns. At small candidate counts independence can't be achieved
    at all — skip the stage rather than fake it (mirrors
    halving/pairing.py's own N=1/N=2 rules for the batch validator-
    pairing path).

    Returns the model that should actually answer this stage's call —
    ``model`` itself for a non-validator stage, a cross-family
    candidate for a validator stage, or ``None`` when the caller should
    skip the stage entirely this round (structurally impossible at the
    current candidate-pool size — counted toward ``n_stages_attempted``
    here since it's not any one candidate's fault, same treatment as a
    cache hit).
    """
    if not stage.is_validator:
        return model
    n_candidates = len(cfg.candidates)
    if n_candidates < 3:
        n_stages_attempted[model] = n_stages_attempted.get(model, 0) + 1
        if n_candidates < 2:
            _log_validator_skip_once(cfg.round_idx, "n1", logging.INFO, "[validator] %s: only %d candidate(s) in the pool - skipping %r (independence requires >= 2)", model, n_candidates, stage.id)
        else:
            _log_validator_skip_once(cfg.round_idx, "n2", logging.WARNING, "[validator] N=2 candidates cannot achieve validator independence (mutual A<->B blame) - skipping %r this round", stage.id)
        return None
    call_model = select_validator_for_producer(
        producer_model=model, candidate_pool=cfg.candidates,
        eff_cost=cfg.halving.last_eff_cost, rng_seed=0,
    )
    if call_model is None:
        # Unreachable in practice (n_candidates >= 3 guarantees >= 2
        # eligible non-producer candidates), but treated the same way
        # defensively rather than silently under-counting coverage if
        # pairing.py's contract ever changes.
        n_stages_attempted[model] = n_stages_attempted.get(model, 0) + 1
        return None
    return call_model


async def _call_llm_guarded(
    *,
    cfg: RoundConfig,
    call_model: str,
    sys_p: str,
    usr_p: str,
    global_sem: asyncio.Semaphore,
    per_op_sems: dict[str, asyncio.Semaphore],
    stage_op: str,
) -> tuple[str | None, dict[str, Any], float, str | None, str | None]:
    """Acquire the global + per-op semaphores, call the provider, and
    classify any exception via ``classify_provider_error`` — the
    semaphore/try/except/finally machinery factored out of
    ``_run_pipeline``'s main loop body.

    Returns ``(response_text, telemetry, duration_sec, error_class,
    error_message)``.
    """
    async with global_sem:
        sem = per_op_sems.get(stage_op)
        if sem is not None:
            await sem.acquire()
        try:
            t0 = time.monotonic()
            response_text, telemetry = await _call_llm(
                cfg=cfg, model=call_model, sys_p=sys_p, usr_p=usr_p,
            )
            duration = time.monotonic() - t0
            error_class = telemetry.get("error_class")
            error_message = telemetry.get("error_message")
        except Exception as e:
            duration = 0.0
            response_text = None
            error_class = classify_provider_error(type(e).__name__, str(e))
            error_message = redact_secrets(str(e))[:500]
            telemetry = {}
        finally:
            if sem is not None:
                sem.release()
    return response_text, telemetry, duration, error_class, error_message


def _maybe_quarantine(ctx: StageContext, cfg: RoundConfig, stage: Stage, duration: float, cost_usd: float) -> None:
    """Storm-detection (StageContext.quarantined — docstring-promised,
    previously unimplemented, audit: 04/09-Medium): an abnormally long
    or costly call marks the rest of THIS pipeline quarantined; checked
    at the top of the next loop iteration in ``_run_pipeline``."""
    if duration > cfg.quarantine_duration_sec or (stage.budget_per_call > 0 and cost_usd > stage.budget_per_call * cfg.quarantine_cost_multiplier):
        ctx.quarantined = True
        ctx.quarantine_reason = f"stage {stage.id!r}: duration={duration:.1f}s cost=${cost_usd:.4f} " f"(thresholds: {cfg.quarantine_duration_sec:.0f}s / " f"{stage.budget_per_call * cfg.quarantine_cost_multiplier:.4f}$)"


def _classify_call_outcome(
    error_class: str | None, response_text: str | None, parsed: Any,
) -> tuple[str | None, bool, bool]:
    """Distinguish "the call itself failed" (``error_class`` already
    set from a provider exception) from "the HTTP call succeeded but
    the response was empty/unparseable/a refusal" — previously
    indistinguishable from a real success: error_class stayed None, the
    row got cached and re-scored as 1.0 forever, and
    ``parse_failure_prefix`` (which exists FOR EXACTLY THIS SIGNAL, per
    ``ResponseParser``'s own docstring) was never populated (audit:
    Critical, multiple reports).

    Returns ``(parse_failure_prefix, money_was_spent, cacheable)``.
    """
    money_was_spent = not error_class
    if not money_was_spent:
        return None, False, False
    if response_text and parsed is None:
        return response_text[:200], True, False
    if not response_text:
        return None, True, False
    return None, True, True


async def _record_prompt_build_failure(
    cfg: RoundConfig, stage: Stage, task_unit: TaskUnit, model: str,
    build_exc: Exception, infra_errors: list[str],
) -> None:
    """A consumer-supplied ``PromptBuilder`` raising must not silently
    ``continue`` past this stage with no trace (audit: 02-High —
    ``pyutilz.dev.code_audit`` itself flagged this ``except Exception``
    as a swallowed-exception pattern, un-actioned in the baseline). A
    bug reproducing only for THIS (stage, task_unit, model) combination
    would otherwise silently shrink ``n_stages_attempted[model]``,
    which feeds the coverage gate (``Halving.promote()``,
    ``coverage_min=0.7`` by default) — a candidate could get eliminated
    for "insufficient coverage" caused by a framework/plugin bug that
    isn't its fault, with nothing in ``RoundResult`` pointing at why.

    No prompt text exists to hash normally (that's exactly what failed
    to build), so the row's identity is a synthetic hash stable per
    (stage, task_unit, model) — a resumed run reproduces/overwrites the
    same row instead of piling up duplicates on every retry.
    """
    synthetic_hash = hash_text(f"__prompt_build_failure__:{stage.id}:{task_unit.id}:{model}")
    row = RunRow(
        composite_hash=synthetic_hash,
        provider=cfg.provider_label,
        model=model,
        thinking=cfg.thinking,
        stage=stage.id,
        task_unit_id=task_unit.id,
        stratum=task_unit.stratum,
        lang=stage.lang,
        parent_prompt_hash=None,
        experiment_tag=cfg.experiment_tag,
        response=None,
        error_class="InternalPipelineError",
        error_message=redact_secrets(str(build_exc))[:500],
        cost_usd=0.0,
        effective_cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        cache_hit_tokens=0,
        cache_write_tokens=0,
        n_attempts=1,
    )
    try:
        await cfg.storage.record_call(row)
    except Exception as persist_exc:
        # Same "infra failure must not vanish silently" treatment as
        # the main persistence try/except below — surfaced via
        # RoundResult.notes rather than lost to logs alone.
        infra_errors.append(f"{stage.id}/{model}/{task_unit.id}: prompt-build-failure record_call raised " f"{type(persist_exc).__name__}: {redact_secrets(str(persist_exc))[:200]}")


def _update_error_history(cfg: RoundConfig, model: str, error_class: str | None, error_count: int) -> tuple[int, bool]:
    """Track error history on the halving driver for the alive/dead
    filter, and the local per-pipeline DEAD-error strike count.

    NOTE: RateLimited is deliberately excluded from the per-pipeline
    3-strikes abort this drives (it's in TRANSIENT_ERROR_CLASSES, not
    counted here) even though it IS in DEAD_ERROR_CLASSES for the
    separate run-scoped ``is_alive_candidate`` threshold in
    ``halving/alive_filter.py`` — a model under transient upstream
    backpressure isn't "broken" the way a ContextOverflow/ModelNotFound
    run is, and pyutilz's own retry/backoff layer may already have
    exhausted its attempts before this exception ever reaches
    ``classify_provider_error``. Confirmed intentional (audit:
    02-Medium asked for this to be documented, not changed).

    Returns ``(new_error_count, should_abort_pipeline)``.
    """
    if not error_class:
        return error_count, False
    cfg.halving.error_history.setdefault(model, []).append(error_class)
    if error_class not in DEAD_ERROR_CLASSES:
        return error_count, False
    error_count += 1
    return error_count, error_count >= 3


async def _run_pipeline(
    *,
    cfg: RoundConfig,
    model: str,
    task_unit: TaskUnit,
    global_sem: asyncio.Semaphore,
    per_op_sems: dict[str, asyncio.Semaphore],
    n_stages_attempted: dict[str, int],
    winner_substrates: dict[tuple[str, str], RunRow],
    topo_order_list: list[Stage],
    seen_prompt_hashes: set[str],
    infra_errors: list[str],
) -> None:
    """Run the StageGraph in topo order for one (model, task_unit) pair."""
    ctx = StageContext(task_unit=task_unit)
    error_count = 0
    for stage in topo_order_list:
        if ctx.quarantined:
            # Storm-detection tripped on an earlier stage in THIS
            # pipeline — stop spending on the remaining stages for this
            # (model, task_unit) pair. Other pairs are unaffected.
            _log_throttled(
                f"quarantine:{model}",
                logging.WARNING,
                "[quarantine] %s/%s: skipping remaining stages (%s)",
                model, task_unit.id, ctx.quarantine_reason,
            )
            return

        _apply_winner_substrate(cfg, stage, task_unit, ctx, winner_substrates)

        # Build prompt.
        try:
            sys_p, usr_p = await _build_prompt(stage, task_unit, ctx)
        except Exception as build_exc:
            _log_throttled(
                f"prompt_build_failed:{model}:{stage.id}",
                logging.ERROR,
                "[stage %s] prompt build failed for %s/%s",
                stage.id, model, task_unit.id,
                exc_info=True,
            )
            await _record_prompt_build_failure(cfg, stage, task_unit, model, build_exc, infra_errors)
            n_stages_attempted[model] = n_stages_attempted.get(model, 0) + 1
            continue

        call_model = _resolve_validator_call_model(cfg, stage, model, n_stages_attempted)
        if call_model is None:
            continue

        # Hash + cache lookup. Keyed on the PRODUCER's identity
        # (`model`, not `call_model`) so a validator-stage row's cache
        # key stays consistent with RunRow.model below — using
        # call_model for one and model for the other would let the
        # in-memory resume cache and the persisted row disagree about
        # their own key.
        _sys_h, _usr_h, comp_h = prompt_hashes(sys_p, usr_p)
        cached = await cfg.resume_cache.get(
            composite_hash=comp_h, provider=cfg.provider_label,
            model=model, thinking=cfg.thinking,
        )
        if cached is not None:
            logger.info("[resume] %s | %s | %s : cache hit", stage.id, task_unit.id, model)
            ctx.outputs[stage.id] = _parse_safely(stage, task_unit, cached.response, cached.input_tokens, cached.output_tokens)
            ctx.parent_prompt_hashes[stage.id] = comp_h
            ctx.response_texts[stage.id] = cached.response
            # A cache hit is a genuine (already-completed) attempt at
            # this stage — it must count toward coverage the same as a
            # live call. The previous code `continue`d here WITHOUT
            # incrementing n_stages_attempted, so a fully-resumed
            # (100% cache-hit) round saw 0/N attempted for every
            # candidate and the coverage gate eliminated the entire
            # pool — the exact scenario "resume cache" exists to serve
            # (audit: Critical, reproduced).
            n_stages_attempted[model] = n_stages_attempted.get(model, 0) + 1
            continue

        # Budget gate.
        reservation = 0.0
        if cfg.budget_gate is not None:
            ok, _spent, _cap, reservation = await cfg.budget_gate.check(
                cfg.storage, cfg.experiment_tag, stage.op,
                stage_budget_per_call=stage.budget_per_call,
            )
            if not ok:
                continue

        # Persist prompt dedup BEFORE the LLM call so a crash mid-call
        # leaves the prompt persisted for inspection. Skipped when this
        # exact hash was already upserted earlier in THIS round (by
        # this or another concurrent pipeline) — upsert_prompts is
        # idempotent regardless, this just avoids a guaranteed-no-op
        # storage round-trip on every repeat (audit: 06-Low).
        if comp_h not in seen_prompt_hashes:
            seen_prompt_hashes.add(comp_h)
            await cfg.storage.upsert_prompts(system_text=sys_p, user_text=usr_p)

        response_text, telemetry, duration, error_class, error_message = await _call_llm_guarded(
            cfg=cfg, call_model=call_model, sys_p=sys_p, usr_p=usr_p,
            global_sem=global_sem, per_op_sems=per_op_sems, stage_op=stage.op,
        )

        # Parse + populate ctx so downstream stages have a substrate.
        parent_h = None
        if stage.parent_stage:
            parent_h = ctx.parent_prompt_hashes.get(stage.parent_stage)
        in_tok = telemetry.get("input_tokens") or 0
        out_tok = telemetry.get("output_tokens") or 0
        rsn_tok = telemetry.get("reasoning_tokens") or 0
        cost_usd = telemetry.get("cost_usd") or 0.0
        eff_cost = telemetry.get("effective_cost_usd") or cost_usd
        parsed = _parse_safely(stage, task_unit, response_text, in_tok, out_tok) if response_text else None
        ctx.outputs[stage.id] = parsed
        ctx.parent_prompt_hashes[stage.id] = comp_h
        ctx.response_texts[stage.id] = response_text or ""

        _maybe_quarantine(ctx, cfg, stage, duration, cost_usd)
        parse_failure_prefix, money_was_spent, cacheable = _classify_call_outcome(error_class, response_text, parsed)

        row = RunRow(
            composite_hash=comp_h,
            provider=cfg.provider_label,
            model=model,
            thinking=cfg.thinking,
            stage=stage.id,
            task_unit_id=task_unit.id,
            stratum=task_unit.stratum,
            lang=stage.lang,
            parent_prompt_hash=parent_h,
            experiment_tag=cfg.experiment_tag,
            response=response_text,
            error_class=error_class,
            error_message=error_message,
            cost_usd=cost_usd,
            effective_cost_usd=eff_cost,
            input_tokens=in_tok,
            output_tokens=out_tok,
            reasoning_tokens=rsn_tok,
            cache_hit_tokens=telemetry.get("cache_hit_tokens"),
            cache_write_tokens=telemetry.get("cache_write_tokens"),
            cache_discount_usd=telemetry.get("cache_discount_usd"),
            is_byok=telemetry.get("is_byok"),
            web_search_citations=telemetry.get("web_search_citations"),
            upstream_resolved_model=telemetry.get("upstream_resolved_model"),
            upstream_provider=telemetry.get("upstream_provider"),
            upstream_model=telemetry.get("upstream_model"),
            native_finish_reason=telemetry.get("native_finish_reason"),
            generation_id=telemetry.get("generation_id"),
            mean_attempt_duration_sec=duration,
            n_attempts=1,
            parse_failure_prefix=parse_failure_prefix,
            generation_details=({"validator_model": call_model} if stage.is_validator and call_model != model else {}),
        )

        # Persistence is wrapped separately from the provider call
        # above: a storage/resume-cache/budget-gate failure here must
        # NOT be indistinguishable from a provider error, and must NOT
        # silently vanish the fact that a call was genuinely attempted
        # (and, if money_was_spent, PAID FOR). The previous code had no
        # try/except here at all — an infra failure propagated straight
        # to _one_pair's outer catch-all, skipping the
        # n_stages_attempted increment below entirely and silently
        # corrupting the coverage gate along with it (audit: Critical,
        # async-concurrency report).
        try:
            await cfg.storage.record_call(row)
            if cfg.budget_gate is not None:
                # Called for EVERY attempted call, success or failure —
                # this is what releases the reservation `check()` made
                # above (cost_usd is already 0.0 for a failed call, so
                # this correctly records "nothing spent" while still
                # reconciling the reservation). Gating this on
                # `money_was_spent` would leave a failed call's
                # reservation stuck forever, permanently shrinking
                # every subsequent check()'s headroom for this op.
                await cfg.budget_gate.record_cost(
                    cfg.experiment_tag, stage.op, cost_usd,
                    effective_cost_usd=eff_cost, reservation=reservation,
                )
            if money_was_spent and cacheable:
                await cfg.resume_cache.put(
                    CachedResponse(
                        composite_hash=comp_h,
                        response=response_text or "",
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        reasoning_tokens=rsn_tok,
                        cost_usd=cost_usd,
                        thinking=cfg.thinking,
                        source_tag=cfg.experiment_tag,
                    ),
                    provider=cfg.provider_label, model=model,
                )
        except Exception as persist_exc:
            _log_throttled(
                f"persist_infra_failure:{model}:{stage.id}",
                logging.ERROR,
                "[stage %s] %s/%s: INFRASTRUCTURE failure persisting a %s "
                "call (storage.record_call / resume_cache.put / "
                "budget_gate.record_cost) - the LLM call itself already "
                "%s. This is NOT a provider/model error and is not "
                "counted against the candidate's error history.",
                stage.id, model, task_unit.id,
                "succeeded" if not error_class else "failed",
                "spent money" if money_was_spent else "cost nothing",
                exc_info=True,
            )
            # Surfaced to the caller via RoundResult.notes (see
            # run_round) — a caller reading only the return value, not
            # logs, must still be able to tell this happened.
            infra_errors.append(f"{stage.id}/{model}/{task_unit.id}: {type(persist_exc).__name__}: {redact_secrets(str(persist_exc))[:200]}")

        # Bookkeeping for halving's coverage gate. Runs regardless of
        # whether persistence above succeeded — the call was genuinely
        # ATTEMPTED (and possibly paid for), so it must count even if
        # we couldn't persist the result.
        n_stages_attempted[model] = n_stages_attempted.get(model, 0) + 1

        # Deliberately keyed on the PROVIDER-CALL outcome (error_class
        # from _call_llm_guarded above), not on the persistence
        # try/except's outcome — an infra failure says nothing about
        # this candidate's health and must not count toward its dead-
        # candidate error budget.
        error_count, should_abort = _update_error_history(cfg, model, error_class, error_count)
        if should_abort:
            _log_throttled(
                f"dead_errors_abort:{model}",
                logging.WARNING,
                "[pipeline] %s/%s: 3+ DEAD errors - " "aborting remaining stages",
                model, task_unit.id,
            )
            return


def _log_validator_skip_once(round_idx: int, reason: str, level: int, msg: str, *args: Any) -> None:
    """Log a validator-independence skip at most once per (round, reason)
    instead of once per (model, task_unit) pipeline in that round —
    otherwise a large candidate pool at N<3 spams an identical line
    dozens of times."""
    key = (round_idx, reason)
    if key in _validator_skip_logged:
        return
    _validator_skip_logged.add(key)
    logger.log(level, msg, *args)


async def _build_prompt(
    stage: Stage, task_unit: TaskUnit, ctx: StageContext,
) -> tuple[str, str]:
    """Call the stage's PromptBuilder; await if it returns a coroutine."""
    result = stage.prompt_builder(
        task_unit=task_unit, ctx=ctx, lang=stage.lang,
    )
    if asyncio.iscoroutine(result):
        result = await result
    if not isinstance(result, tuple) or len(result) != 2:
        raise ValueError(f"PromptBuilder for stage {stage.id!r} must return " f"(system, user) tuple")
    return result


def _parse_safely(
    stage: Stage, task_unit: TaskUnit, response_text: str | None,
    in_tok: int, out_tok: int,
) -> Any:
    """Call the stage's parser, swallowing parser exceptions."""
    if response_text is None:
        return None
    try:
        return stage.parser(
            task_unit=task_unit, response_text=response_text,
            input_tokens=in_tok, output_tokens=out_tok,
        )
    except Exception:
        logger.exception("[parser] %s for task_unit=%s failed", stage.id, task_unit.id)
        return None


async def _call_llm(
    *, cfg: RoundConfig, model: str, sys_p: str, usr_p: str,
) -> tuple[str | None, dict[str, Any]]:
    """Make the actual LLM call via the configured provider factory.

    Default factory uses ``pyutilz.llm.get_llm_provider`` with the
    OpenRouter provider. Returns ``(response_text, telemetry_dict)``
    where telemetry includes input/output/reasoning tokens, costs,
    and Phase-4 OR extras when available.
    """
    if cfg.provider_factory is not None:
        provider = cfg.provider_factory(model)
    else:
        from pyutilz.llm import get_llm_provider
        provider = get_llm_provider(cfg.provider_label, model=model)

    response = await provider.generate(prompt=usr_p, system=sys_p)
    telemetry: dict[str, Any] = {}
    # Pull whatever the provider exposes (pyutilz Phase-4 OR fields).
    for attr, key in [
        ("last_input_tokens", "input_tokens"),
        ("last_output_tokens", "output_tokens"),
        ("last_reasoning_tokens", "reasoning_tokens"),
        ("last_cost_usd", "cost_usd"),
        ("last_effective_cost_usd", "effective_cost_usd"),
        ("last_cache_hit_tokens", "cache_hit_tokens"),
        ("last_cache_write_tokens", "cache_write_tokens"),
        ("last_cache_discount_usd", "cache_discount_usd"),
        ("last_is_byok", "is_byok"),
        ("last_web_search_citations", "web_search_citations"),
        ("last_upstream_resolved_model", "upstream_resolved_model"),
        ("last_upstream_provider", "upstream_provider"),
        ("last_upstream_model", "upstream_model"),
        ("last_native_finish_reason", "native_finish_reason"),
        ("last_generation_id", "generation_id"),
    ]:
        v = getattr(provider, attr, None)
        if v is not None:
            telemetry[key] = v
    return response, telemetry
