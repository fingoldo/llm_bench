"""Executes the README's "Quick start" code block verbatim, end-to-end.

Direct regression test for audit finding 10-High: the README example
previously had two TypeErrors (missing required ``Stage.op``,
``candidates`` passed to ``Benchmark()`` instead of ``run_phase()``)
that nothing in the test suite would have caught, since it was never
actually executed anywhere. This test extracts the code block from
``README.md`` itself (not a hand-copied duplicate that could drift out
of sync) and runs it with fakes standing in for the reader-supplied
``my_pool``/``build_draft``/etc. and the real LLM call — so any future
signature drift between the README and the actual API breaks this
test, not just the two historical bugs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from llm_bench import TaskUnit

_README_PATH = Path(__file__).resolve().parents[2] / "README.md"


def _extract_quickstart_code() -> str:
    text = _README_PATH.read_text(encoding="utf-8")
    match = re.search(r"## Quick start\s*\n```python\n(.*?)```", text, re.DOTALL)
    assert match is not None, "README.md's '## Quick start' python code block not found"
    return match.group(1)


class _FakePool:
    def __init__(self) -> None:
        self.units = [TaskUnit(id=f"u{i}", stratum="v") for i in range(6)]

    def sample(self, n: int) -> list[TaskUnit]:
        return self.units[:n]

    def total_size(self) -> int:
        return len(self.units)


class _FakeProvider:
    def __init__(self, label: str, model: str) -> None:
        self.label = label
        self.model = model
        self.last_cost_usd = 0.001
        self.last_effective_cost_usd = 0.001
        self.last_input_tokens = 10
        self.last_output_tokens = 5
        self.last_reasoning_tokens = 0

    async def generate(self, *, prompt: str, system: str) -> str:
        return '{"ok": true, "padding": "enough length here to clear the default scorer"}'


def _build_draft(*, task_unit, ctx, lang=None):
    return ("sys draft", f"user draft {task_unit.id}")


def _parse_draft(*, task_unit, response_text, input_tokens, output_tokens):
    return {"raw": response_text} if response_text else None


def _build_validate(*, task_unit, ctx, lang=None):
    return ("sys validate", f"user validate {task_unit.id}")


def _parse_validate(*, task_unit, response_text, input_tokens, output_tokens):
    return {"raw": response_text} if response_text else None


async def test_readme_quickstart_example_runs_without_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # README uses a relative FileStorage root

    def _fake_get_llm_provider(label: str, *, model: str) -> _FakeProvider:
        return _FakeProvider(label, model)

    monkeypatch.setattr("pyutilz.llm.get_llm_provider", _fake_get_llm_provider)

    code = _extract_quickstart_code()
    namespace: dict[str, Any] = {
        "my_pool": _FakePool(),
        "build_draft": _build_draft,
        "parse_draft": _parse_draft,
        "build_validate": _build_validate,
        "parse_validate": _parse_validate,
    }
    wrapped = "async def __readme_quickstart():\n" + "\n".join(f"    {line}" for line in code.splitlines()) + "\n    return report\n"
    exec(wrapped, namespace)  # controlled input: this repo's own README.md, not untrusted user data
    report = await namespace["__readme_quickstart"]()

    assert report.experiment_tag == "exp_v1"
    assert len(report.rounds) == 3
