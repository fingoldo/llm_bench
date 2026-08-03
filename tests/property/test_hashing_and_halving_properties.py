"""Property-based (hypothesis) tests for two invariants that table-driven
unit tests can only sample a handful of points of:

  1. Hashing determinism (core/hashing.py) — the ENTIRE resume-cache /
     dedup story depends on ``composite_hash`` being a pure function of
     (system_text, user_text) with no hidden non-determinism (dict
     ordering, encoding quirks, etc.) across arbitrary Unicode input.

  2. ``Halving.promote()``'s promotion-size invariant — "never promotes
     more than the round's target size" (absent specialty-preservation,
     which is deliberately allowed to exceed it — see driver.py) is a
     correctness property that must hold for EVERY (n_candidates,
     target, score distribution) combination, not just the handful of
     hand-picked cases in test_halving.py.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from llm_bench import Halving, HalvingSchedule
from llm_bench.core.hashing import composite_hash, hash_text, prompt_hashes

# ──────────────────────────────────────────────────────────────────────
# Hashing determinism
# ──────────────────────────────────────────────────────────────────────

_text_strategy = st.text(max_size=500)


class TestHashTextProperties:
    @given(text=_text_strategy)
    @settings(max_examples=200)
    def test_deterministic_across_calls(self, text):
        assert hash_text(text) == hash_text(text)

    @given(text=_text_strategy)
    @settings(max_examples=200)
    def test_always_64_hex_chars(self, text):
        h = hash_text(text)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    @given(a=_text_strategy, b=_text_strategy)
    @settings(max_examples=200)
    def test_different_text_different_hash(self, a, b):
        if a != b:
            assert hash_text(a) != hash_text(b)


class TestCompositeHashProperties:
    @given(sys_text=_text_strategy, usr_text=_text_strategy)
    @settings(max_examples=200)
    def test_deterministic_across_calls(self, sys_text, usr_text):
        assert composite_hash(sys_text, usr_text) == composite_hash(sys_text, usr_text)

    @given(sys_text=_text_strategy, usr_text=_text_strategy)
    @settings(max_examples=200)
    def test_split_position_matters(self, sys_text, usr_text):
        # The (system, user) SPLIT is part of the fingerprint, not just
        # the concatenated bytes — moving one character across the
        # system/user boundary must change the hash (this is exactly
        # why the separator byte exists; a naive concat would silently
        # collide "AB"+"C" with "A"+"BC").
        if usr_text:
            moved_sys = sys_text + usr_text[0]
            moved_usr = usr_text[1:]
            if (moved_sys, moved_usr) != (sys_text, usr_text):
                assert composite_hash(sys_text, usr_text) != composite_hash(moved_sys, moved_usr)

    def test_nul_byte_boundary_does_not_collide(self):
        # Direct regression test for a real collision this property
        # suite caught: the original implementation joined system/user
        # bytes with a b"\\x00" separator, which is ambiguous whenever
        # the prompt text itself contains a literal NUL byte —
        # composite_hash("", "\\x00") and composite_hash("\\x00", "")
        # both joined to the identical b"\\x00\\x00" and hashed equal,
        # even though they are two genuinely different prompt pairs.
        # Fixed via a length-prefix on system_text instead of a
        # separator byte.
        assert composite_hash("", "\x00") != composite_hash("\x00", "")

    @given(sys_text=_text_strategy, usr_text=_text_strategy)
    @settings(max_examples=200)
    def test_swap_changes_hash_when_distinct(self, sys_text, usr_text):
        if sys_text != usr_text:
            assert composite_hash(sys_text, usr_text) != composite_hash(usr_text, sys_text)

    @given(sys_text=_text_strategy, usr_text=_text_strategy)
    @settings(max_examples=200)
    def test_prompt_hashes_consistent_with_individual_functions(self, sys_text, usr_text):
        sys_h, usr_h, comp_h = prompt_hashes(sys_text, usr_text)
        assert sys_h == hash_text(sys_text)
        assert usr_h == hash_text(usr_text)
        assert comp_h == composite_hash(sys_text, usr_text)


# ──────────────────────────────────────────────────────────────────────
# Halving.promote — promotion-size invariant
# ──────────────────────────────────────────────────────────────────────

@given(
    n_candidates=st.integers(min_value=2, max_value=30),
    target=st.integers(min_value=1, max_value=30),
    score_seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=100)
def test_promote_never_exceeds_target_size(n_candidates, target, score_seed):
    target = min(target, n_candidates)
    models = [f"m{i}" for i in range(n_candidates)]
    # Deterministic pseudo-random scores from score_seed, no ties needed
    # for this invariant to hold (cost_tiebreak_key breaks ties
    # deterministically either way).
    import random
    rng = random.Random(score_seed)
    scores = {m: rng.random() for m in models}
    eff_cost = {m: 0.01 + rng.random() * 0.1 for m in models}

    sched = HalvingSchedule(round_sizes=(n_candidates, target), units_per_arm=(1, 1))
    h = Halving(schedule=sched)
    result = h.promote(1, scores, eff_cost)

    assert len(result.candidates_out) <= target


@given(
    n_candidates=st.integers(min_value=2, max_value=30),
    target=st.integers(min_value=1, max_value=30),
    score_seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=100)
def test_promote_partitions_all_candidates_without_specialty_or_dead_filter(n_candidates, target, score_seed):
    # No per_stage_winners / no error_history / no coverage gate in
    # play here -> every input candidate must land in EXACTLY one of
    # candidates_out / eliminated, with no candidate silently dropped
    # or duplicated.
    target = min(target, n_candidates)
    models = [f"m{i}" for i in range(n_candidates)]
    import random
    rng = random.Random(score_seed)
    scores = {m: rng.random() for m in models}
    eff_cost = {m: 0.01 + rng.random() * 0.1 for m in models}

    sched = HalvingSchedule(round_sizes=(n_candidates, target), units_per_arm=(1, 1))
    h = Halving(schedule=sched)
    result = h.promote(1, scores, eff_cost)

    out_set = set(result.candidates_out)
    elim_set = set(result.eliminated)
    assert out_set & elim_set == set()
    assert out_set | elim_set == set(models)
    assert len(result.candidates_out) == len(out_set)  # no duplicates
