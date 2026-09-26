"""Per-stage provider kwargs, carved out of `round_runner` to keep it inside the 1000-LOC budget.

Two concerns live here, both about what a single call to a provider is allowed to ask for: assembling the
kwargs a stage declares, and saying something once when a caller declares no output cap at all.
"""

from __future__ import annotations

import logging
from typing import Any

from llm_bench.core.types import Stage

logger = logging.getLogger(__name__)

_WARNED_ONCE: set[str] = set()


def warn_once(message: str) -> None:
    """Log `message` at warning level the first time it is seen, and never again.

    A per-call warning on a fleet run is thousands of identical lines, which is how a real signal becomes
    something operators filter out. Keyed on the message itself, so a second model still gets its own line.
    """
    if message in _WARNED_ONCE:
        return
    _WARNED_ONCE.add(message)
    logger.warning("%s", message)


def stage_generate_kwargs(stage: Stage, *, model: str = "", system: str = "", user: str = "") -> dict[str, Any]:
    """The provider kwargs one stage asks for: its ``json_schema`` plus any ``generate_kwargs``.

    Built here rather than inline so the precedence is stated once and is testable: a key a consumer put in
    ``generate_kwargs`` wins over the framework's own, including ``json_schema`` itself, because a consumer
    who set both meant the explicit one.

    A value that is CALLABLE is resolved per call as ``value(model=..., system=..., user=...)``. One dict per
    stage forces every arm to share a number that is only correct for one of them, and the damage is not
    hypothetical: an arena whose tightest arm capped output at 32,000 tokens imposed that cap on the whole
    fleet, and 39 of 70 captures came back truncated at exactly 31,933. Retiring that arm only moved the
    number - the next run's ceiling of 54,853 was already binding on an arm whose own limit is 131,072. The
    caller here knows the model and both prompts, so a consumer that can compute its own budget should be
    able to say so rather than hand over a floor.
    """
    kwargs: dict[str, Any] = {}
    if stage.json_schema is not None:
        kwargs["json_schema"] = stage.json_schema
    kwargs.update(stage.generate_kwargs)
    # An explicit loop rather than a comprehension: the branch coverage gate cross-references a branch's own
    # line number against the lines coverage.py recorded, and a ternary inside a comprehension reports a line
    # the runtime never attributes an arc to - so a covered branch read as an uncovered one.
    resolved: dict[str, Any] = {}
    for key, value in kwargs.items():
        if callable(value):
            resolved[key] = value(model=model, system=system, user=user)
        else:
            resolved[key] = value
    return resolved
