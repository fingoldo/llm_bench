"""Telemetry must come from the provider's own published snapshot, not from guessed attribute names."""

from __future__ import annotations

from typing import Any

import pytest

from llm_bench.runner.round_runner import _call_llm


class _PyutilzShapedProvider:
    """The real shape: token counts and cost live under names the getattr sweep does not know."""

    def __init__(self) -> None:
        self._last_usage = {"input_tokens": 1200, "output_tokens": 56, "reasoning_tokens": 300}
        self.last_actual_cost_usd = 0.0042
        self.last_upstream_provider = "AtlasCloud"

    async def generate(self, *, prompt: str, system: str = "", **_kw: Any) -> str:
        return '{"ok": true}'

    def last_call_summary(self) -> dict[str, Any]:
        return {
            "input_tokens": self._last_usage["input_tokens"],
            "output_tokens": self._last_usage["output_tokens"],
            "reasoning_tokens": self._last_usage["reasoning_tokens"],
            "cost_usd": self.last_actual_cost_usd,
            "upstream_provider": self.last_upstream_provider,
        }


class _NoSummaryProvider:
    last_input_tokens = 7
    last_cost_usd = 0.5

    async def generate(self, *, prompt: str, system: str = "", **_kw: Any) -> str:
        return "hi"


class _BrokenSummaryProvider:
    last_input_tokens = 7

    async def generate(self, *, prompt: str, system: str = "", **_kw: Any) -> str:
        return "hi"

    def last_call_summary(self) -> dict[str, Any]:
        raise RuntimeError("snapshot exploded")


def _cfg(provider: Any) -> Any:
    return type("Cfg", (), {"provider_factory": staticmethod(lambda _m: provider), "provider_label": "fake"})()


@pytest.mark.asyncio
class TestCostIsActuallyHarvested:
    async def test_a_real_call_reports_its_tokens_and_cost(self):
        # Measured before this fix: every row of an 8-model run showed 0 tokens and $0.00 while carrying
        # real responses, which left `BudgetGate` and quarantine's cost limb permanently inert.
        _resp, telemetry = await _call_llm(cfg=_cfg(_PyutilzShapedProvider()), model="m", sys_p="s", usr_p="u")
        assert telemetry["input_tokens"] == 1200
        assert telemetry["output_tokens"] == 56
        assert telemetry["cost_usd"] == 0.0042

    async def test_a_provider_without_a_snapshot_still_works(self):
        _resp, telemetry = await _call_llm(cfg=_cfg(_NoSummaryProvider()), model="m", sys_p="s", usr_p="u")
        assert telemetry["input_tokens"] == 7 and telemetry["cost_usd"] == 0.5

    async def test_a_broken_snapshot_never_costs_the_response(self, caplog):
        resp, telemetry = await _call_llm(cfg=_cfg(_BrokenSummaryProvider()), model="m", sys_p="s", usr_p="u")
        assert resp == "hi"
        assert telemetry["input_tokens"] == 7

    async def test_a_response_with_no_output_tokens_is_warned_about(self, caplog):
        import logging

        caplog.set_level(logging.WARNING)
        await _call_llm(cfg=_cfg(_NoSummaryProvider()), model="m", sys_p="s", usr_p="u")
        assert any("no output tokens" in r.getMessage() for r in caplog.records)
