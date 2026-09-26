"""Route policy reaches the default provider factory and the recorded identity (DS-10); stream interruptions and
require_parameters 404s get their own failure labels (DS-11)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pyutilz.llm

from llm_bench import Benchmark, HalvingSchedule, InMemoryStorage, Stage, StageGraph, TaskUnit
from llm_bench.halving.alive_filter import DEAD_ERROR_CLASSES, TRANSIENT_ERROR_CLASSES, is_alive_candidate
from llm_bench.runner.classify import classify_provider_error
from llm_bench.runner.round_runner import provider_identity, run_round
from tests.unit.test_round_runner import TAG, _cfg, _graph_single_stage, _init, _units

QUANT = {"provider_quantizations": ("bf16", "fp16", "fp8", "unknown")}


@dataclass
class _Provider:
    model: str

    async def generate(self, *, prompt: str, system: str, **_kw) -> str:
        return '{"ok": true, "padding": "enough length here"}'


@pytest.fixture
def factory_calls(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake(label, **kwargs):
        calls.append((label, kwargs))
        return _Provider(model=kwargs["model"])

    monkeypatch.setattr(pyutilz.llm, "get_llm_provider", fake)
    return calls


# DS-10 -----------------------------------------------------------------------------------------------------------------


def test_provider_identity():
    assert provider_identity("openrouter", {}) == "openrouter"
    assert provider_identity("openrouter", None) == "openrouter"
    a = provider_identity("openrouter", QUANT)
    assert a.startswith("openrouter{route:") and a != "openrouter"
    assert a == provider_identity("openrouter", dict(QUANT))
    assert a != provider_identity("openrouter", {"provider_quantizations": ("fp4",)})


async def test_default_factory_receives_provider_kwargs_and_rows_record_the_route(factory_calls):
    storage = await _init(InMemoryStorage())
    cfg = _cfg(candidates=["m1"], task_units=_units(1), stages=_graph_single_stage(), storage=storage)
    cfg.provider_kwargs = dict(QUANT)
    await run_round(cfg)
    assert factory_calls and all(call == ("openrouter", {"model": "m1", **QUANT}) for call in factory_calls)
    rows = [r async for r in storage.query_rows(experiment_tag=TAG)]
    assert rows and {r.provider for r in rows} == {provider_identity("openrouter", QUANT)}


async def test_no_kwargs_keeps_plain_label(factory_calls):
    storage = await _init(InMemoryStorage())
    await run_round(_cfg(candidates=["m1"], task_units=_units(1), stages=_graph_single_stage(), storage=storage))
    assert factory_calls[0] == ("openrouter", {"model": "m1"})
    assert {r.provider async for r in storage.query_rows(experiment_tag=TAG)} == {"openrouter"}


async def test_custom_factory_ignores_kwargs_in_identity():
    storage = await _init(InMemoryStorage())
    cfg = _cfg(candidates=["m1"], task_units=_units(1), stages=_graph_single_stage(), storage=storage, provider_factory=_Provider)
    cfg.provider_kwargs = dict(QUANT)
    await run_round(cfg)
    assert {r.provider async for r in storage.query_rows(experiment_tag=TAG)} == {"openrouter"}


@dataclass
class _Pool:
    units: list[TaskUnit]

    def sample(self, n: int) -> list[TaskUnit]:
        return self.units[:n]

    def total_size(self) -> int:
        return len(self.units)


def _pb(*, task_unit, ctx, lang=None):
    return ("sys", "user")


def _parser(*, task_unit, response_text, input_tokens, output_tokens):
    return None if response_text is None else {"raw": response_text}


async def test_benchmark_passes_provider_kwargs_to_rounds_and_preflight(factory_calls, monkeypatch):
    units = [TaskUnit(id="u0", stratum="v")]
    bench = Benchmark(
        task_pool=_Pool(units), stages=StageGraph([Stage(id="enrich", op="enrich", prompt_builder=_pb, parser=_parser)]),
        storage=InMemoryStorage(), halving_schedule=HalvingSchedule(round_sizes=(1,), units_per_arm=(1,), pool_size=1),
        provider_kwargs=dict(QUANT),
    )
    async with bench:
        await bench.preflight(["m1"])
        assert factory_calls[-1] == ("openrouter", {"model": "m1", **QUANT})
        factory_calls.clear()
        await bench.run_phase(tag="t", candidates=["m1"], units=units)
    assert factory_calls and all(kw == {"model": "m1", **QUANT} for _, kw in factory_calls)


# DS-11 -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "OpenRouter API error 404: No endpoints found that can handle the requested parameters.",
        "no endpoint honours require_parameters for this request",
    ],
)
def test_require_parameters_404_is_parameters_unsupported_not_model_not_found(message):
    assert classify_provider_error("LLMProviderError", message) == "ParametersUnsupported"


def test_plain_no_endpoints_is_still_model_not_found():
    assert classify_provider_error("LLMProviderError", "OpenRouter API error 404: No endpoints found for x/y.") == "ModelNotFound"


def test_stream_interruption_label_is_transient():
    assert classify_provider_error("LLMStreamInterruptedError", "upstream server_error; Provider returned error") == "StreamInterrupted"
    assert "StreamInterrupted" in TRANSIENT_ERROR_CLASSES
    assert "StreamInterrupted" not in DEAD_ERROR_CLASSES
    assert "ParametersUnsupported" in DEAD_ERROR_CLASSES
    # Three interruptions are backpressure-like, not a dead model; six are.
    assert is_alive_candidate("m", error_history={"m": ["StreamInterrupted"] * 3})[0] is True
    assert is_alive_candidate("m", error_history={"m": ["StreamInterrupted"] * 6})[0] is False

