"""LLMProvider Protocol conformance tests.

Previously had ZERO test coverage anywhere in the repository (surfaced
by ``tests/test_meta/test_test_source_parity.py``) even though the
module's own docstring cites audit finding 01-Medium: the provider
contract used to exist only as free-text docstrings and a bare
``getattr(provider, attr, None)`` loop in ``round_runner.py``, formalised
here as a ``@runtime_checkable Protocol``. ``tests/unit/test_round_runner.py``
exercises fake providers structurally (duck-typed ``generate()``) but
never imports or checks against ``LLMProvider`` itself -- these tests
pin the actual structural contract: what satisfies ``isinstance(x,
LLMProvider)`` and what doesn't.
"""

from __future__ import annotations

import inspect

from llm_bench.provider.base import LLMProvider


class _ConformingProvider:
    async def generate(self, *, prompt: str, system: str, max_tokens: int | None = None) -> str:
        return "response"


class _MissingGenerate:
    async def other_method(self) -> str:
        return "not it"


class _GenerateIsNotAsync:
    def generate(self, *, prompt: str, system: str, max_tokens: int | None = None) -> str:
        return "sync, not a coroutine function"


class TestLLMProviderConformance:
    def test_class_with_generate_satisfies_protocol(self):
        assert isinstance(_ConformingProvider(), LLMProvider)

    def test_class_without_generate_does_not_satisfy_protocol(self):
        assert not isinstance(_MissingGenerate(), LLMProvider)

    def test_runtime_checkable_only_checks_method_presence_not_async(self):
        # @runtime_checkable structural checks only verify the attribute
        # exists and is callable -- it can't inspect whether it's a
        # coroutine function. A sync method named "generate" still
        # satisfies isinstance(); this test documents that limitation
        # rather than asserting a stronger guarantee the Protocol can't
        # actually provide.
        assert isinstance(_GenerateIsNotAsync(), LLMProvider)

    def test_generate_is_declared_as_a_coroutine_function(self):
        # Direct check on the Protocol's own declaration (independent of
        # runtime_checkable's structural limitation above): the contract
        # itself is async.
        assert inspect.iscoroutinefunction(LLMProvider.generate)

    async def test_conforming_provider_generate_is_awaitable(self):
        provider: LLMProvider = _ConformingProvider()
        result = await provider.generate(prompt="hello", system="be nice")
        assert result == "response"
