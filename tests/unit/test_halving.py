"""Halving unit tests — pruner, schedule, assignment, pairing, driver.

These cover the framework's elimination logic in isolation. Integration
with storage / runner lands in Phase E's smoke tests.
"""

from __future__ import annotations

import statistics

import pytest

from llm_bench import (
    DEAD_ERROR_CLASSES,
    Halving,
    HalvingSchedule,
    TRANSIENT_ERROR_CLASSES,
    allowed_validator_pairs,
    assign_task_units_for_round,
    cost_tiebreak_key,
    is_alive_candidate,
    mad_bootstrap_prune,
    select_validator_for_producer,
)
from llm_bench.halving import pairing as pairing_module
from llm_bench.halving.pairing import _model_family

# ──────────────────────────────────────────────────────────────────────
# HalvingSchedule
# ──────────────────────────────────────────────────────────────────────

class TestHalvingSchedule:
    def test_default_shape(self):
        s = HalvingSchedule()
        assert s.round_sizes == (52, 26, 13, 7)
        assert s.units_per_arm == (4, 8, 12, 12)

    def test_n_calls_for_stage(self):
        s = HalvingSchedule()
        # Round 1: 52 candidates × 4 units/arm
        assert s.n_calls_for_stage(1) == 208
        # Round 4: 7 × 12
        assert s.n_calls_for_stage(4) == 84

    def test_n_calls_clamps_pool(self):
        s = HalvingSchedule()
        # pool=8 < units_per_arm=12 → clamp
        assert s.n_calls_for_stage(4, pool_size=8) == 7 * 8

    def test_n_calls_out_of_range(self):
        s = HalvingSchedule()
        with pytest.raises(ValueError, match="round_idx 0 out of range"):
            s.n_calls_for_stage(0)
        with pytest.raises(ValueError, match="round_idx 5 out of range"):
            s.n_calls_for_stage(5)

    def test_next_round_size(self):
        s = HalvingSchedule()
        assert s.next_round_size(1) == 26  # round 1 done → round 2 target
        assert s.next_round_size(2) == 13
        assert s.next_round_size(3) == 7
        # Last round → 1 final winner
        assert s.next_round_size(4) == 1

    def test_next_round_size_bounds(self):
        s = HalvingSchedule()
        with pytest.raises(ValueError):
            s.next_round_size(0)
        with pytest.raises(ValueError):
            s.next_round_size(5)

    def test_total_calls(self):
        s = HalvingSchedule()
        # 52*4 + 26*8 + 13*12 + 7*12 = 208 + 208 + 156 + 84
        assert s.total_calls_for_stage() == 656

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(ValueError, match="same length"):
            HalvingSchedule(round_sizes=(10, 5), units_per_arm=(2, 4, 6))

    def test_empty_round_sizes_rejected(self):
        with pytest.raises(ValueError, match="must not be empty"):
            HalvingSchedule(round_sizes=(), units_per_arm=())

    def test_nonpositive_round_sizes_rejected(self):
        with pytest.raises(ValueError, match="round_sizes must be all positive"):
            HalvingSchedule(round_sizes=(10, 0), units_per_arm=(2, 4))

    def test_negative_round_size_rejected(self):
        with pytest.raises(ValueError, match="round_sizes must be all positive"):
            HalvingSchedule(round_sizes=(10, -5), units_per_arm=(2, 4))

    def test_nonpositive_units_per_arm_rejected(self):
        with pytest.raises(ValueError, match="units_per_arm must be all positive"):
            HalvingSchedule(round_sizes=(10, 5), units_per_arm=(2, 0))

    def test_nonpositive_pool_size_rejected(self):
        with pytest.raises(ValueError, match="pool_size must be positive"):
            HalvingSchedule(round_sizes=(10, 5), units_per_arm=(2, 4), pool_size=0)

    def test_growing_round_sizes_warns_but_does_not_raise(self, caplog):
        with caplog.at_level("WARNING"):
            s = HalvingSchedule(round_sizes=(5, 10), units_per_arm=(2, 4))
        assert s.round_sizes == (5, 10)  # construction succeeds
        assert any("not monotonically non-increasing" in r.message for r in caplog.records)


# ──────────────────────────────────────────────────────────────────────
# mad_bootstrap_prune
# ──────────────────────────────────────────────────────────────────────

class TestMADBootstrapPrune:
    def test_empty(self):
        survivors, lb = mad_bootstrap_prune({})
        assert survivors == set()
        assert lb == {}

    def test_single(self):
        survivors, lb = mad_bootstrap_prune({"m1": 0.5})
        assert survivors == {"m1"}
        assert lb == {"m1": 0.5}

    def test_n_le_3_keeps_all(self):
        # No statistical power below n=4
        survivors, _ = mad_bootstrap_prune({"a": 0.5, "b": 0.6, "c": 0.4})
        assert survivors == {"a", "b", "c"}

    def test_constant_cluster_keeps_all(self):
        # mad=0 AND stdev=0 => everyone survives.
        survivors, _ = mad_bootstrap_prune({f"m{i}": 0.5 for i in range(5)})
        assert len(survivors) == 5

    def test_single_outlier_in_constant_cluster_pruned(self):
        # mad=0 but stdev > 0 — outlier branch fires with 1.5x threshold.
        scores = {f"m{i}": 0.7 for i in range(5)}
        scores["bad"] = 0.10
        survivors, _ = mad_bootstrap_prune(scores)
        assert "bad" not in survivors
        assert len(survivors) == 5  # the 5 cluster members

    def test_near_zero_mad_from_float_noise_treated_as_constant_cluster(self):
        # Direct regression test for math.isclose(mad, 0.0, abs_tol=1e-9)
        # replacing an exact `mad == 0.0` comparison (audit: 03-Medium):
        # upstream float accumulation (mean-of-mean divisions in
        # round_runner.py) can land a "conceptually constant" cluster a
        # few ULPs off an exact zero MAD. Confirmed this input actually
        # produces a nonzero-but-tiny mad (~2.22e-16, not exactly 0.0)
        # before asserting the observable outcome: it must still route
        # through the forgiving constant-cluster fallback (all 5 survive
        # as one cluster), not build an absurdly tight threshold from a
        # near-zero MAD that would treat sub-ULP noise as a real signal.
        eps = 2.220446049250313e-16  # sys.float_info.epsilon
        base = 0.7
        scores = {
            "a": base, "b": base + eps, "c": base - eps,
            "d": base + 2 * eps, "e": base - 2 * eps,
        }
        survivors, _ = mad_bootstrap_prune(scores)
        assert survivors == set(scores)

    def test_normal_distribution_outlier_pruned(self):
        # 9 cluster + 1 outlier. mad > 0, normal bootstrap path.
        scores = {
            "a": 0.50, "b": 0.52, "c": 0.48, "d": 0.55, "e": 0.49,
            "f": 0.51, "g": 0.53, "h": 0.47, "i": 0.50,
            "bad": 0.05,
        }
        survivors, _ = mad_bootstrap_prune(scores)
        assert "bad" not in survivors

    def test_paired_branch_outlier_pruned(self):
        # per_task_unit_scores present for every candidate -> paired
        # bootstrap path (audit: 06-High #2 perf fix touched this
        # branch directly; the unpaired fallback above was already
        # covered but the paired branch itself had zero test coverage).
        per_unit = {
            "a": [0.50, 0.52, 0.48, 0.51],
            "b": [0.55, 0.53, 0.54, 0.56],
            "c": [0.49, 0.47, 0.50, 0.48],
            "d": [0.51, 0.50, 0.52, 0.49],
            "bad": [0.05, 0.06, 0.04, 0.05],
        }
        scores = {m: statistics.fmean(v) for m, v in per_unit.items()}
        survivors, lower_ci = mad_bootstrap_prune(scores, per_task_unit_scores=per_unit)
        assert "bad" not in survivors
        assert set(survivors) == {"a", "b", "c", "d"}
        assert set(lower_ci) == set(scores)

    def test_paired_branch_falls_back_when_one_candidate_missing_units(self):
        # A candidate with an EMPTY per_task_unit_scores entry disables
        # the paired path entirely (use_paired requires every model to
        # have a non-empty list) and the function must still return a
        # coherent, non-crashing result via the unpaired fallback.
        per_unit = {
            "a": [0.50, 0.52, 0.48, 0.51],
            "b": [0.55, 0.53, 0.54, 0.56],
            "c": [0.49, 0.47, 0.50, 0.48],
            "d": [],
        }
        scores = {"a": 0.50, "b": 0.55, "c": 0.48, "d": 0.20}
        survivors, lower_ci = mad_bootstrap_prune(scores, per_task_unit_scores=per_unit)
        assert set(lower_ci) == set(scores)
        assert "d" not in survivors

    def test_paired_branch_cost_penalty_can_flip_survivor(self):
        # eff_cost feeds a cost_penalty into the SAME effective-score
        # used for the paired bootstrap median/threshold — an expensive
        # candidate tied on raw score with a cheap one should be more
        # likely to drop out once cost is accounted for.
        per_unit = {
            "cheap": [0.50, 0.51, 0.49, 0.50],
            "pricey": [0.50, 0.51, 0.49, 0.50],
            "c": [0.48, 0.49, 0.47, 0.48],
            "d": [0.52, 0.53, 0.51, 0.52],
        }
        scores = {m: statistics.fmean(v) for m, v in per_unit.items()}
        eff_cost = {"cheap": 0.01, "pricey": 10.0, "c": 0.01, "d": 0.01}
        _survivors, lower_ci = mad_bootstrap_prune(
            scores, per_task_unit_scores=per_unit, eff_cost=eff_cost, cost_penalty_factor=0.5,
        )
        # cost penalty must have pulled pricey's effective lower-CI
        # below cheap's despite an identical raw score.
        assert lower_ci["pricey"] < lower_ci["cheap"]

    def test_paired_branch_variance_penalty_prunes_equal_mean_spiky_scorer(self):
        # Direct test that the variance penalty changes the outcome
        # relative to the aggregate MEAN alone: "spiky" has the exact
        # same raw mean (0.525) as 4 stable peers, but alternates
        # 0.95/0.10 across task units instead of holding steady near
        # 0.525 -- the coefficient-of-variation penalty must make it
        # lose despite the tied raw score.
        per_unit = {
            "s1": [0.52, 0.53, 0.52, 0.53],
            "s2": [0.53, 0.52, 0.53, 0.52],
            "s3": [0.52, 0.53, 0.53, 0.52],
            "s4": [0.53, 0.52, 0.52, 0.53],
            "spiky": [0.95, 0.10, 0.95, 0.10],
        }
        scores = {m: statistics.fmean(v) for m, v in per_unit.items()}
        assert scores["spiky"] == pytest.approx(scores["s1"])  # tied raw mean
        survivors, lower_ci = mad_bootstrap_prune(scores, per_task_unit_scores=per_unit)
        assert "spiky" not in survivors
        assert {"s1", "s2", "s3", "s4"} <= survivors
        assert lower_ci["spiky"] < lower_ci["s1"]

    def test_paired_branch_agrees_with_aggregate_on_unambiguous_outlier(self):
        # "Basic sanity check" per the audit's own recommendation,
        # refined after investigation: the paired and aggregate
        # (unpaired) branches are NOT guaranteed to produce IDENTICAL
        # survivor sets even when every candidate's per-task-unit
        # scores are internally constant -- verified by hand that they
        # legitimately diverge on a borderline candidate (paired's
        # zero-within-candidate-variance case collapses to a
        # deterministic deviation with no bootstrap shrinkage; the
        # aggregate path always resamples the pooled cross-candidate
        # distribution and picks up real bootstrap noise). What SHOULD
        # hold regardless of which axis is bootstrapped: an
        # unambiguous outlier -- far below every peer, not
        # borderline -- gets pruned by both paths identically.
        per_unit = {
            "a": [0.9] * 4, "b": [0.8] * 4, "c": [0.7] * 4, "d": [0.6] * 4,
            "bad": [0.1] * 4,
        }
        scores = {m: statistics.fmean(v) for m, v in per_unit.items()}
        survivors_paired, _ = mad_bootstrap_prune(scores, per_task_unit_scores=per_unit)
        survivors_agg, _ = mad_bootstrap_prune(scores)
        assert "bad" not in survivors_paired
        assert "bad" not in survivors_agg


# ──────────────────────────────────────────────────────────────────────
# is_alive_candidate
# ──────────────────────────────────────────────────────────────────────

class TestAliveCandidate:
    def test_no_errors(self):
        ok, why = is_alive_candidate("m1")
        assert ok is True and why is None

    def test_no_history_for_model(self):
        ok, _why = is_alive_candidate("m1", error_history={"other": ["ContextOverflow"]})
        assert ok is True

    def test_3_permanent_errors_dead(self):
        ok, why = is_alive_candidate(
            "m1", error_history={"m1": ["ContextOverflow"] * 3},
        )
        assert ok is False
        assert why is not None and "permanent" in why

    def test_2_permanent_errors_alive(self):
        ok, _ = is_alive_candidate(
            "m1", error_history={"m1": ["ContextOverflow"] * 2},
        )
        assert ok is True

    def test_transient_threshold_doubles_default(self):
        # Default transient_threshold = timeout_threshold * 2 = 6
        ok, _ = is_alive_candidate(
            "m1", error_history={"m1": ["RateLimited"] * 5},
        )
        assert ok is True
        ok2, _ = is_alive_candidate(
            "m1", error_history={"m1": ["RateLimited"] * 6},
        )
        assert ok2 is False

    def test_modelnotfound_in_dead_set(self):
        # Single ModelNotFound triggers dead at threshold=1
        ok, _ = is_alive_candidate(
            "m1", error_history={"m1": ["ModelNotFound"]},
            timeout_threshold=1,
        )
        assert ok is False

    def test_transient_classes_meaningful(self):
        # TRANSIENT is intentionally NOT a subset of DEAD: ``RateLimited``
        # lives only in TRANSIENT (not DEAD) so it counts toward the
        # transient-bucket threshold (default 6) without inflating the
        # permanent-bucket threshold (default 3). The AND-NOT in
        # ``is_alive_candidate`` partitions errors cleanly.
        assert "RateLimited" in TRANSIENT_ERROR_CLASSES
        assert "RateLimited" not in DEAD_ERROR_CLASSES
        # ContextOverflow is permanent-only (in DEAD, not in TRANSIENT).
        assert "ContextOverflow" in DEAD_ERROR_CLASSES
        assert "ContextOverflow" not in TRANSIENT_ERROR_CLASSES
        # Timeouts are in BOTH (permanent enum + transient threshold) —
        # but the AND-NOT means they only contribute to transient_count.
        assert "LLMCallTimeout" in DEAD_ERROR_CLASSES
        assert "LLMCallTimeout" in TRANSIENT_ERROR_CLASSES


# ──────────────────────────────────────────────────────────────────────
# assign_task_units_for_round
# ──────────────────────────────────────────────────────────────────────

class TestAssignTaskUnits:
    def test_round1_takes_first_n(self):
        cands = ["m1", "m2", "m3"]
        pool = [f"u{i}" for i in range(12)]
        out = assign_task_units_for_round(cands, pool, 1)
        # Default schedule: round 1 -> 4 units
        for m in cands:
            assert out[m] == ["u0", "u1", "u2", "u3"]

    def test_round4_takes_full(self):
        cands = ["m1"]
        pool = [f"u{i}" for i in range(12)]
        out = assign_task_units_for_round(cands, pool, 4)
        assert out["m1"] == pool

    def test_pool_undersized_clamps(self, caplog):
        cands = ["m1"]
        pool = ["u0", "u1"]
        with caplog.at_level("WARNING"):
            out = assign_task_units_for_round(cands, pool, 1)
        assert out["m1"] == pool
        assert any("clamping" in r.message.lower() for r in caplog.records)


# ──────────────────────────────────────────────────────────────────────
# allowed_validator_pairs
# ──────────────────────────────────────────────────────────────────────

class TestValidatorPairs:
    def test_n_lt_2_empty(self):
        assert allowed_validator_pairs([]) == []
        assert allowed_validator_pairs(["only"]) == []

    def test_n_eq_2_returns_empty_with_warning(self, caplog):
        with caplog.at_level("WARNING"):
            pairs = allowed_validator_pairs(["a/m1", "b/m2"])
        assert pairs == []
        assert any("N=2" in r.message for r in caplog.records)

    def test_n_eq_3_cyclic(self):
        cands = ["a/m1", "b/m2", "c/m3"]
        pairs = allowed_validator_pairs(cands)
        assert len(pairs) == 3
        # Producer != validator for each pair
        for p, v in pairs:
            assert p != v

    def test_n_ge_4_no_self(self):
        cands = [f"v{i}/m{i}" for i in range(5)]
        pairs = allowed_validator_pairs(cands, rng_seed=42)
        for p, v in pairs:
            assert p != v

    def test_n_ge_4_no_self_across_many_seeds(self):
        # Anti-self-judgment is a Layer-1 hard rule (cross-family
        # validator independence) -- run enough distinct seeds/pool
        # sizes that a rare RNG-dependent self-pairing bug would show
        # up rather than surviving on a single lucky seed.
        for n in range(4, 12):
            cands = [f"v{i}/m{i}" for i in range(n)]
            for seed in range(10):
                pairs = allowed_validator_pairs(cands, rng_seed=seed)
                for p, v in pairs:
                    assert p != v, f"self-pairing at n={n} seed={seed}: {p} validates itself"

    def test_every_candidate_appears_as_producer(self):
        cands = [f"v{i}/m{i}" for i in range(6)]
        pairs = allowed_validator_pairs(cands, rng_seed=1)
        producers = {p for p, _ in pairs}
        assert producers == set(cands)

    def test_n_ge_4_no_mirrored_pairs(self):
        # Direct regression test for the used_validators_for guard
        # (pairing.py) that avoids assigning BOTH (A,B) and (B,A) --
        # two candidates mutually validating each other is a weaker
        # independence signal than a spread-out assignment, even
        # though neither pair is a self-judgment. Swept across many
        # seeds/pool sizes so a rare RNG-dependent collision would
        # surface rather than surviving on one lucky seed.
        for n in range(4, 12):
            cands = [f"v{i}/m{i}" for i in range(n)]
            for seed in range(10):
                pairs = allowed_validator_pairs(cands, rng_seed=seed)
                pair_set = set(pairs)
                for p, v in pairs:
                    assert (v, p) not in pair_set, f"mirrored pair at n={n} seed={seed}: ({p},{v}) and ({v},{p}) both present"


# ──────────────────────────────────────────────────────────────────────
# _model_family — unmapped-vendor dedup log (audit: 02-Low)
# ──────────────────────────────────────────────────────────────────────

class TestModelFamily:
    def _isolated_logged_set(self, monkeypatch):
        # _unmapped_vendors_logged is module-level global state shared
        # across the whole test session -- isolate it per test so an
        # earlier test's vendor doesn't silently suppress this one's
        # "first time seen" assertion.
        fresh: set[str] = set()
        monkeypatch.setattr(pairing_module, "_unmapped_vendors_logged", fresh)
        return fresh

    def test_known_vendor_resolves_without_logging(self, monkeypatch, caplog):
        self._isolated_logged_set(monkeypatch)
        with caplog.at_level("INFO"):
            family = _model_family("openai/gpt-4o-mini")
        assert family  # some real family string
        assert not any("not in _VENDOR_TO_FAMILY" in r.message for r in caplog.records)

    def test_unknown_vendor_falls_back_to_raw_prefix(self, monkeypatch):
        self._isolated_logged_set(monkeypatch)
        assert _model_family("brand_new_vendor_xyz/some-model") == "brand_new_vendor_xyz"

    def test_unknown_vendor_logs_once(self, monkeypatch, caplog):
        logged = self._isolated_logged_set(monkeypatch)
        with caplog.at_level("INFO"):
            _model_family("another_unmapped_vendor/model-a")
        assert "another_unmapped_vendor" in logged
        assert sum("another_unmapped_vendor" in r.message for r in caplog.records) == 1

    def test_unknown_vendor_does_not_relog_on_second_call(self, monkeypatch, caplog):
        self._isolated_logged_set(monkeypatch)
        with caplog.at_level("INFO"):
            _model_family("repeat_vendor/model-a")
            _model_family("repeat_vendor/model-b")
        assert sum("repeat_vendor" in r.message for r in caplog.records) == 1


# ──────────────────────────────────────────────────────────────────────
# select_validator_for_producer
# ──────────────────────────────────────────────────────────────────────

class TestSelectValidator:
    def test_returns_none_when_no_other(self):
        assert select_validator_for_producer("m1", ["m1"]) is None
        assert select_validator_for_producer("m1", []) is None

    def test_prefers_cross_family(self):
        # Producer is anthropic; pool has same-family + cross-family
        v = select_validator_for_producer(
            "anthropic/claude-1",
            ["anthropic/claude-2", "openai/gpt-1", "google/gem-1"],
        )
        # Cross-family preferred; among equal cost (no eff_cost), alpha order.
        assert v in {"google/gem-1", "openai/gpt-1"}
        assert not v.startswith("anthropic/")

    def test_cheapest_among_ties(self):
        v = select_validator_for_producer(
            "anthropic/c1",
            ["openai/expensive", "google/cheap"],
            eff_cost={"openai/expensive": 0.10, "google/cheap": 0.01},
        )
        assert v == "google/cheap"


# ──────────────────────────────────────────────────────────────────────
# cost_tiebreak_key
# ──────────────────────────────────────────────────────────────────────

class TestCostTiebreakKey:
    def test_higher_score_better(self):
        # Higher score => smaller sort key (model b ahead of a).
        ka = cost_tiebreak_key("a", 0.5, eff_cost={"a": 0.01, "b": 0.01})
        kb = cost_tiebreak_key("b", 0.7, eff_cost={"a": 0.01, "b": 0.01})
        assert kb < ka

    def test_cheaper_breaks_score_tie(self):
        ka = cost_tiebreak_key("a", 0.5, eff_cost={"a": 0.05, "b": 0.01})
        kb = cost_tiebreak_key("b", 0.5, eff_cost={"a": 0.05, "b": 0.01})
        assert kb < ka

    def test_missing_cost_sorts_last(self):
        ka = cost_tiebreak_key("a", 0.5, eff_cost={"b": 0.01})
        kb = cost_tiebreak_key("b", 0.5, eff_cost={"b": 0.01})
        assert kb < ka  # 'a' has +inf cost

    def test_latency_multiplies_cost(self):
        # Same $/call but a 2x faster => a wins.
        ka = cost_tiebreak_key(
            "a", 0.5, eff_cost={"a": 0.01, "b": 0.01},
            mean_latency_sec={"a": 1.0, "b": 2.0},
        )
        kb = cost_tiebreak_key(
            "b", 0.5, eff_cost={"a": 0.01, "b": 0.01},
            mean_latency_sec={"a": 1.0, "b": 2.0},
        )
        assert ka < kb


# ──────────────────────────────────────────────────────────────────────
# Halving.promote — end-to-end driver
# ──────────────────────────────────────────────────────────────────────

class TestHalvingPromote:
    def test_top_half_promoted(self):
        sched = HalvingSchedule(
            round_sizes=(4, 2, 1), units_per_arm=(2, 4, 4),
        )
        h = Halving(schedule=sched)
        scores = {"a": 0.9, "b": 0.8, "c": 0.7, "d": 0.6}
        eff_cost = {"a": 0.01, "b": 0.01, "c": 0.01, "d": 0.01}
        # After round 1 -> next round target = round_sizes[1] = 2
        result = h.promote(1, scores, eff_cost)
        # Top 2 by score (and equal cost) survive
        assert set(result.candidates_out) == {"a", "b"}
        assert set(result.eliminated) == {"c", "d"}

    def test_specialty_preserved(self):
        sched = HalvingSchedule(
            round_sizes=(4, 2, 1), units_per_arm=(2, 4, 4),
        )
        h = Halving(schedule=sched)
        scores = {"a": 0.9, "b": 0.8, "c": 0.7, "specialist": 0.30}
        eff_cost = {m: 0.01 for m in scores}
        # specialist would be cut by halving but won "rare_stage"
        result = h.promote(
            1, scores, eff_cost,
            per_stage_winners={"rare_stage": "specialist"},
        )
        assert "specialist" in result.candidates_out
        assert "specialist" in result.specialty_preserved
        assert result.specialty_preserved["specialist"] == "rare_stage"

    def test_coverage_gate_drops_low_coverage(self):
        sched = HalvingSchedule(
            round_sizes=(2, 1), units_per_arm=(2, 2),
        )
        h = Halving(schedule=sched)
        scores = {"good": 0.8, "patchy": 0.95}  # patchy has high score but ran nothing
        eff_cost = {m: 0.01 for m in scores}
        result = h.promote(
            1, scores, eff_cost,
            n_stages_attempted={"good": 10, "patchy": 1},
            total_stages=10,
            coverage_min=0.7,
        )
        assert "patchy" in result.eliminated
        assert "below coverage_min" in result.eliminated_reasons["patchy"]
        assert "good" in result.candidates_out

    def test_dead_filter_overrides_score(self):
        # next_round_size(1) = round_sizes[1] = 2, so both 'top_but_dead'
        # and 'ok' clear the halving cut initially; the dead filter then
        # removes 'top_but_dead' from `promoted` post-hoc, leaving 'ok'.
        sched = HalvingSchedule(round_sizes=(3, 2), units_per_arm=(2, 2))
        h = Halving(
            schedule=sched,
            error_history={"top_but_dead": ["ContextOverflow"] * 5},
        )
        scores = {"top_but_dead": 0.95, "ok": 0.5}
        eff_cost = {m: 0.01 for m in scores}
        result = h.promote(1, scores, eff_cost)
        # top_but_dead was on track to win but is dead-marked
        assert "top_but_dead" in result.eliminated
        assert "dead" in result.eliminated_reasons["top_but_dead"]
        assert "ok" in result.candidates_out

    def test_dead_filter_does_not_refill_slot(self):
        # Documented quirk: dead filter is post-hoc, so when the dead
        # candidate took the only slot, that slot stays empty rather
        # than auto-promoting the next-best. Operators see this as
        # "round had 1 winner; winner died; round produced 0 winners".
        sched = HalvingSchedule(round_sizes=(2, 1), units_per_arm=(2, 2))
        h = Halving(
            schedule=sched,
            error_history={"top_but_dead": ["ContextOverflow"] * 5},
        )
        scores = {"top_but_dead": 0.95, "ok": 0.5}
        eff_cost = {m: 0.01 for m in scores}
        result = h.promote(1, scores, eff_cost)
        # Both eliminated: top_but_dead by dead filter, ok by halving cut.
        assert result.candidates_out == []
        assert "dead" in result.eliminated_reasons["top_but_dead"]

    def test_multi_specialty_bonus(self):
        sched = HalvingSchedule(
            round_sizes=(4, 2, 1), units_per_arm=(2, 4, 4),
        )
        h = Halving(schedule=sched)
        scores = {"a": 0.50, "b": 0.45, "c": 0.40, "d": 0.30}
        eff_cost = {m: 0.01 for m in scores}
        # b wins 2 stages — gets +0.05 bonus
        result = h.promote(
            1, scores, eff_cost,
            per_stage_winners={"s1": "b", "s2": "b"},
        )
        assert result.scores["b"] == pytest.approx(0.50)  # 0.45 + 0.05

    def test_empty_scores(self):
        h = Halving()
        result = h.promote(1, {}, {})
        assert result.candidates_out == []
        assert result.eliminated == []

    def test_single_candidate_in_scores_dict_survives(self):
        # Direct test of the literal edge case named by the audit:
        # scores={"only": 0.5} (a round starting with exactly ONE
        # candidate total, not a halving cut narrowing down TO one) --
        # exercises mad_bootstrap_prune's own n==1 short-circuit
        # (returns {"only"} unconditionally) plus the halving cut and
        # dead/coverage filters all degenerating gracefully to a no-op
        # on a singleton input, without raising or mis-eliminating it.
        h = Halving()
        result = h.promote(1, {"only": 0.5}, {"only": 0.01})
        assert result.candidates_out == ["only"]
        assert result.eliminated == []

    def test_single_survivor_final_round(self):
        # Final round (just_finished_round_idx == len(round_sizes)) ->
        # next_round_size returns 1: exactly the single best candidate
        # must survive, with no specialty-preservation force-promotion
        # of extras (the fix for the final-round over-promotion bug).
        sched = HalvingSchedule(round_sizes=(3, 1), units_per_arm=(2, 2))
        h = Halving(schedule=sched)
        scores = {"a": 0.9, "b": 0.5, "c": 0.4}
        eff_cost = {m: 0.01 for m in scores}
        result = h.promote(2, scores, eff_cost)
        assert result.candidates_out == ["a"]
        assert set(result.eliminated) == {"b", "c"}

    def test_single_survivor_final_round_ignores_specialty(self):
        sched = HalvingSchedule(round_sizes=(3, 1), units_per_arm=(2, 2))
        h = Halving(schedule=sched)
        scores = {"a": 0.9, "b": 0.5, "specialist": 0.1}
        eff_cost = {m: 0.01 for m in scores}
        result = h.promote(
            2, scores, eff_cost,
            per_stage_winners={"rare_stage": "specialist"},
        )
        # Final round has exactly 1 slot; specialty preservation must
        # NOT force a 2nd winner into candidates_out here.
        assert result.candidates_out == ["a"]
        assert "specialist" not in result.candidates_out
