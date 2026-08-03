"""Stage-level Protocols: prompt building, response parsing, gold checking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from llm_bench.core.types import TaskUnit

# ──────────────────────────────────────────────────────────────────────
# Stage execution context — what prompt builders & parsers see
# ──────────────────────────────────────────────────────────────────────

@dataclass
class StageContext:
    """Mutable state passed through a single (model, task_unit) pipeline.

    Each stage's PromptBuilder reads upstream stage results from
    ``outputs`` (keyed by parent stage id). Stage parsers write their
    parsed output into ``outputs`` so downstream stages can read it.

    Carries quarantine state (set by storm-detection logic in
    ``runner/round_runner.py``: an abnormally long/expensive stage call
    relative to this pipeline's own running average sets
    ``quarantined=True`` and stops the remaining stages for that pair)
    so a runaway reasoning chain on one stage cancels the rest of the
    pipeline for that pair instead of continuing to spend on it.
    """
    task_unit: TaskUnit
    outputs: dict[str, Any] = field(default_factory=dict)
    """Stage id → parsed result. Populated as the pipeline advances."""

    parent_prompt_hashes: dict[str, str] = field(default_factory=dict)
    """Stage id → composite_hash of the parent stage's prompt. Used to
    chain ``parent_prompt_hash`` on the row write."""

    response_texts: dict[str, str] = field(default_factory=dict)
    """Stage id → raw response text. Useful for parsers that recover
    from JSON errors via the upgraded extract_json."""

    quarantined: bool = False
    quarantine_reason: str | None = None


# ──────────────────────────────────────────────────────────────────────
# Stage Protocols
# ──────────────────────────────────────────────────────────────────────

@runtime_checkable
class PromptBuilder(Protocol):
    """Builds the (system_prompt, user_prompt) pair for one stage call.

    Returns a tuple. Coroutines are also acceptable — the runner
    ``await``s the result.
    """

    def __call__(
        self, *, task_unit: TaskUnit, ctx: StageContext, lang: str | None = None,
    ) -> tuple[str, str]:
        ...


@runtime_checkable
class ResponseParser(Protocol):
    """Parses a raw LLM response text into a stage-specific result object.

    Returning ``None`` signals an unrecoverable parse failure — the
    pipeline records the row with ``parse_failure_prefix`` set and
    proceeds to the next stage with ``ctx.outputs[stage_id] = None``.
    """

    def __call__(
        self, *, task_unit: TaskUnit, response_text: str | None,
        input_tokens: int, output_tokens: int,
    ) -> Any | None:
        ...


@runtime_checkable
class GoldChecker(Protocol):
    """Optional accuracy check against a known-good reference.

    When the framework is given a GoldChecker, ``compute_ranking`` blends
    a 30% gold-accuracy term into the model score (the other 70% comes
    from validator-pair scoring). When None, the gold blend is skipped
    entirely and validator-pair carries the score — that's the path the
    JobApp POC uses.

    ``score`` returns a float in ``[0, 1]`` measuring how close the
    response matches the reference. The exact metric is consumer-defined
    (VocabApp: per-field correctness counters; JobApp: keyword recall).
    """

    async def score(
        self, *, task_unit: TaskUnit, stage_id: str, response: str | None,
    ) -> float:
        ...
