"""TaskPool Protocol — abstracts the source of TaskUnits being benchmarked."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol, runtime_checkable

from llm_bench.core.types import TaskUnit


@runtime_checkable
class TaskPool(Protocol):
    """Source of TaskUnits to benchmark.

    Examples:
        - VocabAppSynsetPool wraps OEWN synset queries
        - JobPool reads cover-letter jobs from a CSV / API

    The framework calls ``sample(n)`` once per round to get the working
    pool slice. Stratified samplers should pull from each stratum
    proportionally; non-stratified samplers can use any deterministic
    order.

    Implementations MAY be async-aware (a coroutine method works too —
    the runner ``await``s any awaitable result). The return types below
    reflect that directly (``X | Awaitable[X]``, matching ``RowScorer``'s
    own async-aware Protocol in ``ranking/ranker.py``) rather than just
    promising it in prose — a synchronous-only signature here would
    make any implementation written exactly to this docstring fail
    mypy despite being fully supported at runtime (audit: 04-High, the
    original bug report; the type-level half of the fix landed
    2026-07-22 once `tests/` started being mypy-checked too).
    """

    def sample(self, n: int) -> list[TaskUnit] | Awaitable[list[TaskUnit]]:
        """Return up to ``n`` task units. Should be deterministic given
        the same seed for reproducibility (callers re-seed via
        ``rng_seed`` argument when needed)."""
        ...

    def total_size(self) -> int | Awaitable[int]:
        """Total available task units. Used by the runner to clamp
        ``n_per_arm`` against the actual pool ceiling."""
        ...
