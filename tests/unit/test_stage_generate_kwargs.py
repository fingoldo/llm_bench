"""A stage's ``json_schema``/``generate_kwargs`` reach the provider.

Why this is worth pinning: a benchmark predicts production behaviour, and a consumer whose production path
constrains the model with a strict schema while the benchmark leaves it free-form is measuring a different
system. Before this, ``_call_llm`` called ``provider.generate(prompt=..., system=...)`` and there was no
way to pass a schema at all, so the gap was silent and invisible in the report.
"""

from __future__ import annotations

from typing import Any

import pytest

from llm_bench.core.types import Stage
from llm_bench.runner.round_runner import _call_llm, _stage_generate_kwargs

SCHEMA: dict[str, Any] = {"name": "s", "strict": True, "schema": {"type": "object", "properties": {}}}


def _stage(**over: Any) -> Stage:
    base: dict[str, Any] = {"id": "x", "op": "x", "prompt_builder": lambda **_: ("", ""), "parser": lambda **_: None}
    base.update(over)
    return Stage(**base)


class TestStageGenerateKwargs:
    def test_a_stage_with_no_schema_asks_for_nothing(self):
        assert _stage_generate_kwargs(_stage()) == {}

    def test_the_schema_is_forwarded_under_its_provider_name(self):
        assert _stage_generate_kwargs(_stage(json_schema=SCHEMA)) == {"json_schema": SCHEMA}

    def test_extra_knobs_ride_along(self):
        kwargs = _stage_generate_kwargs(_stage(json_schema=SCHEMA, generate_kwargs={"temperature": 0.1, "max_tokens": 4096}))
        assert kwargs == {"json_schema": SCHEMA, "temperature": 0.1, "max_tokens": 4096}

    def test_an_explicit_generate_kwarg_beats_the_stage_field(self):
        # A consumer who set both meant the explicit one; silently preferring the framework's copy would
        # make the override unusable and the reason undiscoverable.
        other = {"name": "other", "strict": False, "schema": {}}
        assert _stage_generate_kwargs(_stage(json_schema=SCHEMA, generate_kwargs={"json_schema": other}))["json_schema"] is other

    def test_two_stages_do_not_share_a_kwargs_dict(self):
        a, b = _stage(), _stage()
        a.generate_kwargs["temperature"] = 0.9
        assert b.generate_kwargs == {}


class _RecordingProvider:
    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    async def generate(self, *, prompt: str, system: str = "", **kwargs: Any) -> str:
        self.seen = {"prompt": prompt, "system": system, **kwargs}
        return "{}"


class _StrictProvider:
    """A provider that does not accept extra kwargs - the shape a stale consumer package would have."""

    async def generate(self, *, prompt: str, system: str = "") -> str:
        return "{}"


@pytest.mark.asyncio
class TestTheProviderActuallyReceivesIt:
    async def test_schema_and_knobs_arrive_at_generate(self, monkeypatch):
        provider = _RecordingProvider()
        cfg = type("Cfg", (), {"provider_factory": staticmethod(lambda _m: provider), "provider_label": "fake"})()
        await _call_llm(cfg=cfg, model="m", sys_p="sys", usr_p="usr", generate_kwargs={"json_schema": SCHEMA, "temperature": 0.1})
        assert provider.seen["json_schema"] is SCHEMA
        assert provider.seen["temperature"] == 0.1
        assert provider.seen["prompt"] == "usr" and provider.seen["system"] == "sys"

    async def test_no_kwargs_keeps_the_previous_call_shape(self):
        provider = _RecordingProvider()
        cfg = type("Cfg", (), {"provider_factory": staticmethod(lambda _m: provider), "provider_label": "fake"})()
        await _call_llm(cfg=cfg, model="m", sys_p="sys", usr_p="usr")
        assert set(provider.seen) == {"prompt", "system"}

    async def test_a_provider_that_cannot_take_the_schema_raises_rather_than_dropping_it(self):
        # Loud beats silent: the alternative - swallowing the TypeError and calling without the schema -
        # would report a score for output that was never constrained, which is the exact
        # measuring-a-different-system failure this feature exists to prevent.
        cfg = type("Cfg", (), {"provider_factory": staticmethod(lambda _m: _StrictProvider()), "provider_label": "fake"})()
        with pytest.raises(TypeError):
            await _call_llm(cfg=cfg, model="m", sys_p="s", usr_p="u", generate_kwargs={"json_schema": SCHEMA})


class TestACallableValueIsResolvedPerModel:
    """One dict per stage forces every arm to share a number correct for only one of them.

    Measured on the autopsia bycatch arena: the tightest arm capped output at 32,000 tokens and that cap
    became the whole fleet's, so 39 of 70 captures came back truncated at exactly 31,933. Retiring that arm
    only moved the number - the next run's 54,853 ceiling was already binding on `qwen/qwen3.8-flash`, whose
    own limit is 131,072 and which emitted 53,389 on its first clean capture.
    """

    def test_the_callable_sees_the_model_and_both_prompts(self):
        seen: dict[str, str] = {}

        def budget(*, model: str, system: str, user: str) -> int:
            seen.update(model=model, system=system, user=user)
            return 131_072

        kwargs = _stage_generate_kwargs(
            _stage(generate_kwargs={"max_tokens": budget}),
            model="qwen/qwen3.8-flash", system="SYS", user="USR",
        )
        assert kwargs["max_tokens"] == 131_072
        assert seen == {"model": "qwen/qwen3.8-flash", "system": "SYS", "user": "USR"}

    def test_two_models_get_two_different_budgets_from_one_stage(self):
        caps = {"a/roomy": 131_072, "b/tight": 32_000}
        stage = _stage(generate_kwargs={"max_tokens": lambda *, model, system, user: caps[model]})

        assert _stage_generate_kwargs(stage, model="a/roomy")["max_tokens"] == 131_072
        assert _stage_generate_kwargs(stage, model="b/tight")["max_tokens"] == 32_000

    def test_a_plain_value_is_still_passed_through_untouched(self):
        """The callable form is opt-in; nothing that worked before changes shape."""
        kwargs = _stage_generate_kwargs(_stage(generate_kwargs={"temperature": 0.1, "max_tokens": 4096}))
        assert kwargs == {"temperature": 0.1, "max_tokens": 4096}

    def test_a_callable_json_schema_is_resolved_too(self):
        """No key is special-cased: a consumer that varies its schema by model is the same mechanism."""
        stage = _stage(generate_kwargs={"json_schema": lambda *, model, system, user: {"for": model}})
        assert _stage_generate_kwargs(stage, model="a/one")["json_schema"] == {"for": "a/one"}


class TestAnUncappedGenerationGetsAnExplicitCeiling:
    """`max_tokens` absent means the provider's implicit "use my maximum" - an unbounded cost and an
    unbounded wall-clock, which on a fleet run is the difference between one slow arm and a round that
    never ends. The number is usually the same either way; what changes is that it becomes a stated value
    in the request instead of an absent field whose consequence is discovered from the invoice.
    """

    class _Provider:
        max_output_tokens = 131_072

        def __init__(self) -> None:
            self.seen: dict[str, object] = {}

        async def generate(self, **kwargs: object) -> str:
            self.seen = kwargs
            return "{}"

    class _SilentProvider(_Provider):
        max_output_tokens = 0

    def _cfg(self, provider):
        from llm_bench.runner.round_runner import RoundConfig

        cfg = RoundConfig.__new__(RoundConfig)
        object.__setattr__(cfg, "provider_factory", lambda model: provider)
        return cfg

    def test_the_providers_own_ceiling_is_sent_when_the_caller_named_none(self):
        import asyncio

        from llm_bench.runner.round_runner import _call_llm

        provider = self._Provider()
        asyncio.run(_call_llm(cfg=self._cfg(provider), model="a/one", sys_p="s", usr_p="u"))
        assert provider.seen["max_tokens"] == 131_072

    def test_a_caller_who_named_one_keeps_it(self):
        import asyncio

        from llm_bench.runner.round_runner import _call_llm

        provider = self._Provider()
        asyncio.run(
            _call_llm(cfg=self._cfg(provider), model="a/one", sys_p="s", usr_p="u", generate_kwargs={"max_tokens": 4096})
        )
        assert provider.seen["max_tokens"] == 4096

    def test_a_provider_that_states_no_ceiling_keeps_the_old_behaviour_and_says_so(self, caplog):
        """Silence would be the wrong answer twice: the call is genuinely unbounded, and nothing said so."""
        import asyncio
        import logging

        from llm_bench.runner import stage_kwargs
        from llm_bench.runner.round_runner import _call_llm

        stage_kwargs._WARNED_ONCE.clear()
        provider = self._SilentProvider()
        with caplog.at_level(logging.WARNING):
            asyncio.run(_call_llm(cfg=self._cfg(provider), model="a/silent", sys_p="s", usr_p="u"))
        assert "max_tokens" not in provider.seen
        assert "unbounded" in caplog.text

    def test_the_warning_is_said_once_not_once_per_call(self):
        """Thousands of identical lines is how a real signal becomes something operators filter out."""
        import asyncio
        import logging

        from llm_bench.runner import stage_kwargs
        from llm_bench.runner.round_runner import _call_llm

        stage_kwargs._WARNED_ONCE.clear()
        provider = self._SilentProvider()
        with caplog_at(logging.WARNING) as records:
            for _ in range(3):
                asyncio.run(_call_llm(cfg=self._cfg(provider), model="a/silent", sys_p="s", usr_p="u"))
        assert len([r for r in records if "unbounded" in r.getMessage()]) == 1

import contextlib
import logging as _logging


@contextlib.contextmanager
def caplog_at(level):
    """A record collector that survives repeated calls, which `caplog` inside a loop does not make obvious."""
    records: list[_logging.LogRecord] = []

    class _Collector(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level)
    root = _logging.getLogger("llm_bench.runner.stage_kwargs")
    previous = root.level
    root.addHandler(handler)
    root.setLevel(level)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)

