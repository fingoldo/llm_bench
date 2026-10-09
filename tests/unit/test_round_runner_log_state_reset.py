"""The conftest autouse fixture clears round_runner's process-level log-once state after each test."""

from __future__ import annotations

import logging

from llm_bench.runner import round_runner as rr


def test_first_test_dirties_state(caplog):
    with caplog.at_level(logging.WARNING):
        rr._log_validator_skip_once(7, "r", logging.WARNING, "skip")
    for _ in range(5):
        rr._log_throttled("k", logging.WARNING, "boom")
    assert (7, "r") in rr._validator_skip_logged
    assert rr._throttle_hit_counts["k"] == 5


def test_second_test_sees_clean_state(caplog):
    assert not rr._validator_skip_logged
    assert not rr._throttle_hit_counts
    with caplog.at_level(logging.WARNING):
        rr._log_validator_skip_once(7, "r", logging.WARNING, "skip")
    assert len([r for r in caplog.records if r.getMessage() == "skip"]) == 1
