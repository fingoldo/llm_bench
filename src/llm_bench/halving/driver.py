"""Halving state machine — coordinates the multi-round elimination.

After each round the driver picks survivors via:

  1. **Coverage gate** (P0.2): models that attempted < 70% of stages
     are dropped BEFORE statistical pruning so they don't contaminate
     the median / spread used by O1.
  2. **O1 MAD-bootstrap prune**: ``mad_bootstrap_prune`` removes
     statistical outliers below ``median - 2.5 * spread``.
  3. **Halving cut**: top half by ``cost_tiebreak_key(score, cost,
     latency)``. Among same-score peers, cheapest survives; among
     same-cost peers, fastest wins.
  4. **Specialist preservation** (P0.1): per-stage winners that the
     halving cut eliminated are force-promoted with a "kept_for_specialty"
     reason. A model that wins one rare stage but lags on aggregate
     mean is rescued — without this, niche specialists silently die.
  5. **Multi-specialty bonus**: a model winning >= 2 stages in a round
     gets +0.05 added to its score for downstream rounds.
  6. **Alive/dead filter**: candidates with too many error_class hits
     in this run are dropped via ``is_alive_candidate``. Applied AFTER
     specialty preservation — a dead specialist still gets removed
     because dead means the stage can't be delivered at all.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from llm_bench.core.scoring import cost_tiebreak_key
from llm_bench.halving.alive_filter import is_alive_candidate
from llm_bench.halving.pruner import mad_bootstrap_prune
from llm_bench.halving.schedule import HalvingSchedule


@dataclass
class RoundResult:
    """Output of one Halving round.

    ``candidates_in`` lists every candidate that entered the round
    (including coverage-excluded ones — they appear in ``eliminated``
    too). ``candidates_out`` lists the promoted set going into the
    next round. ``specialty_preserved`` flags models that survived
    only via the per-stage-winner force-promote rule (mapping
    ``model -> ", ".join(stage_ids)``).
    """
    round_idx: int
    stage: str
    candidates_in: list[str]
    candidates_out: list[str]
    eliminated: list[str]
    eliminated_reasons: dict[str, str]
    scores: dict[str, float]
    effective_cost_per_call: dict[str, float]
    notes: list[str] = field(default_factory=list)
    specialty_preserved: dict[str, str] = field(default_factory=dict)


@dataclass
class Halving:
    """Sequential Halving state machine spanning multiple rounds.

    The driver is intentionally stateful for ``survivors_by_round``
    (so a multi-round run can reconstruct the full cascade) and for
    ``error_history`` (so the alive/dead filter has run-scoped error
    accumulation). Persistence of survivors / winners across rounds
    goes through the storage backend, not this dataclass.
    """
    schedule: HalvingSchedule = field(default_factory=HalvingSchedule)
    survivors_by_round: dict[int, list[str]] = field(default_factory=dict)
    error_history: dict[str, list[str]] = field(default_factory=dict)
    last_eff_cost: dict[str, float] = field(default_factory=dict)
    """Effective cost per model from the MOST RECENTLY completed round's
    ``promote()`` call. Round 1 has no prior-round data, so this starts
    empty. Consumed by ``round_runner.py`` when picking a cross-family
    validator for a producer (``select_validator_for_producer``'s
    ``eff_cost`` tie-break) — reusing this existing stateful object
    (already threaded through every round) avoids adding yet another
    field to ``RoundConfig`` just to carry one round's lag behind."""
    last_score_bonus: dict[str, float] = field(default_factory=dict)
    """Multi-specialty bonus earned in the MOST RECENTLY completed
    round (see ``promote()``'s "Multi-specialty bonus" section) —
    ``round_runner.py`` adds this into the NEXT round's aggregate
    scores before calling ``promote()`` again, which is what actually
    makes the bonus affect a promotion decision instead of sitting
    unread in a returned ``RoundResult``."""

    def promote(
        self,
        just_finished_round_idx: int,
        scores: dict[str, float],
        eff_cost: dict[str, float],
        *,
        per_stage_winners: dict[str, str] | None = None,
        n_stages_attempted: dict[str, int] | None = None,
        total_stages: int | None = None,
        coverage_min: float = 0.7,
        mean_latency_sec: dict[str, float] | None = None,
        per_task_unit_scores: dict[str, list[float]] | None = None,
    ) -> RoundResult:
        """Apply the Halving rule + O1 prune after the given round.

        ``just_finished_round_idx`` is 1-indexed and refers to the round
        JUST FINISHED. Promoting after round k => next round's target
        size = ``schedule.round_sizes[k]`` (0-indexed under the hood),
        encapsulated by ``schedule.next_round_size``.

        ``per_task_unit_scores`` (``{model: [score_per_task_unit, ...]}``)
        is forwarded to ``mad_bootstrap_prune``, activating its paired-
        bootstrap variance/cost penalty instead of the plain unpaired
        fallback. Without it (the caller's choice not to build this
        cross-stage breakdown), O1 pruning still runs — just on the
        simpler aggregate-only path (audit: 02-High — this parameter
        used to not exist on ``promote()`` at all, so the penalty could
        never fire from the one real caller, ``round_runner.py``).

        The driver does not mutate any external storage — caller
        persists the returned RoundResult.
        """
        if not scores:
            return RoundResult(
                round_idx=just_finished_round_idx, stage="(aggregate)",
                candidates_in=[], candidates_out=[], eliminated=[],
                eliminated_reasons={}, scores={}, effective_cost_per_call={},
            )

        target_size = self.schedule.next_round_size(just_finished_round_idx)

        # Coverage gate: drop low-coverage models BEFORE statistical
        # prune so they don't skew O1's median / spread.
        coverage_excluded: dict[str, str] = {}
        if n_stages_attempted is not None and total_stages is not None and total_stages > 0 and coverage_min > 0:
            for m in list(scores):
                attempted = n_stages_attempted.get(m, 0)
                cov = attempted / total_stages
                if cov < coverage_min:
                    coverage_excluded[m] = f"below coverage_min: {attempted}/{total_stages} " f"= {cov:.0%} < {coverage_min:.0%}"
            if coverage_excluded:
                scores = {m: s for m, s in scores.items() if m not in coverage_excluded}
                eff_cost = {m: c for m, c in eff_cost.items() if m not in coverage_excluded}
                if mean_latency_sec is not None:
                    mean_latency_sec = {m: latency for m, latency in mean_latency_sec.items() if m not in coverage_excluded}

        if not scores:
            # Every model fell below coverage_min - degenerate round.
            return RoundResult(
                round_idx=just_finished_round_idx, stage="(aggregate)",
                candidates_in=list(coverage_excluded), candidates_out=[],
                eliminated=list(coverage_excluded),
                eliminated_reasons=coverage_excluded,
                scores={}, effective_cost_per_call={},
            )

        # O1 statistical prune. per_task_unit_scores/eff_cost activate
        # the variance/cost penalty terms (paired-bootstrap when a
        # per-task-unit breakdown is available) instead of the plain
        # aggregate-only fallback.
        pruner_units = None
        if per_task_unit_scores is not None:
            pruner_units = {m: v for m, v in per_task_unit_scores.items() if m in scores}
        o1_survivors, _lb = mad_bootstrap_prune(scores, per_task_unit_scores=pruner_units, eff_cost=eff_cost)

        # Halving cut: top half (or N/2 rounded up) by cost_tiebreak_key.
        sorted_models = sorted(
            scores.items(),
            key=lambda kv: cost_tiebreak_key(
                kv[0], kv[1], eff_cost=eff_cost,
                mean_latency_sec=mean_latency_sec,
            ),
        )
        halving_survivors = [m for m, _ in sorted_models[:target_size]]

        promoted = [m for m in halving_survivors if m in o1_survivors]
        eliminated_set = set(scores) - set(promoted)
        reasons: dict[str, str] = {}
        for m in eliminated_set:
            if m not in o1_survivors:
                reasons[m] = "O1 MAD-bootstrap below 2.5x spread"
            else:
                reasons[m] = "below halving cutoff"

        # Specialist preservation (P0.1): force-promote per-stage
        # winners that the aggregate cut eliminated. Runs BEFORE the
        # alive/dead filter so a winning-but-error-prone model still
        # gets dropped (alive/dead is a safety signal; specialty is a
        # value signal).
        #
        # Force-promotion is SKIPPED on the final round —
        # ``HalvingSchedule.next_round_size``'s own docstring promises
        # "For the LAST round, returns 1 (a single final winner)", but
        # this rule ran unconditionally regardless of round, so a
        # specialist eliminated by the final halving cut could still
        # get force-promoted back in, leaving ``candidates_out`` with
        # 2+ "final winners" and silently breaking that contract (audit:
        # 03-Medium). ``wins_per_model``/``specialty_kept`` are still
        # computed on the final round (useful reporting signal even
        # when not acted on), just not used to reopen the cut.
        is_final_round = just_finished_round_idx == len(self.schedule.round_sizes)
        specialty_kept: dict[str, str] = {}
        wins_per_model: dict[str, int] = defaultdict(int)
        if per_stage_winners:
            for stage_id, winner_model in per_stage_winners.items():
                if not winner_model:
                    continue
                if winner_model not in scores:
                    continue  # filtered out by coverage_min
                wins_per_model[winner_model] += 1
                if winner_model in specialty_kept:
                    specialty_kept[winner_model] += f", {stage_id}"
                else:
                    specialty_kept[winner_model] = stage_id
                if not is_final_round and winner_model not in promoted:
                    promoted.append(winner_model)
                    eliminated_set.discard(winner_model)
                    reasons.pop(winner_model, None)

        # Multi-specialty bonus: >=2 stage wins => +0.05 to score for
        # downstream rounds. Bonus applies to a copy so the input dict
        # isn't mutated. `self.last_score_bonus` is what actually
        # carries this into the NEXT round: round_runner.py reads it
        # back when building the next round's aggregate_scores before
        # calling promote() again. Previously nothing ever read
        # `scores_with_bonus`/this bonus back — `RoundResult.scores`
        # was returned to the caller but round_runner.py only consumed
        # `candidates_out` from it and rebuilt aggregate_scores from
        # scratch next round via a fresh compute_ranking() call, so the
        # bonus never influenced a single actual promotion decision
        # (audit: 03-Medium).
        bonus_per_model: dict[str, float] = {m: 0.05 for m, n_wins in wins_per_model.items() if n_wins >= 2}
        scores_with_bonus = {m: min(1.0, s + bonus_per_model.get(m, 0.0)) for m, s in scores.items()}

        # Alive/dead filter — overrides above reasons since "dead" is
        # a stronger signal than score-based elimination.
        for m in list(promoted):
            alive, why = is_alive_candidate(m, error_history=self.error_history)
            if not alive:
                promoted.remove(m)
                eliminated_set.add(m)
                reasons[m] = why or "dead"
                # Don't let specialty_preserved overlap with eliminated.
                specialty_kept.pop(m, None)

        # Merge coverage-excluded into eliminated_set with their reasons.
        for m, why in coverage_excluded.items():
            eliminated_set.add(m)
            reasons[m] = why

        candidates_in_full = list(scores) + list(coverage_excluded)
        result = RoundResult(
            round_idx=just_finished_round_idx,
            stage="(aggregate)",
            candidates_in=candidates_in_full,
            candidates_out=promoted,
            eliminated=list(eliminated_set),
            eliminated_reasons=reasons,
            scores=dict(scores_with_bonus),
            effective_cost_per_call=dict(eff_cost),
            specialty_preserved=specialty_kept,
        )
        self.survivors_by_round[just_finished_round_idx] = promoted
        self.last_eff_cost = dict(eff_cost)
        self.last_score_bonus = dict(bonus_per_model)
        return result
