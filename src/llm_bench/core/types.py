"""Core dataclasses shared across the framework.

Imported by every other subpackage. NOTHING in this module may import
from sibling subpackages — keeps the dependency layering clean (core
sits at the bottom, all roads point downward).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NewType
from collections.abc import Callable

# ──────────────────────────────────────────────────────────────────────
# Aliases
# ──────────────────────────────────────────────────────────────────────

ExperimentTag = NewType("ExperimentTag", str)
"""Run label. Filters DB queries; rows under different tags share resume cache
but not aggregation. Convention: ``<consumer>_<purpose>_<unix_ts>``, e.g.
``glossum_phase1_1715173241``."""

_EXPERIMENT_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")


def validate_experiment_tag(tag: str) -> str:
    """Reject an ``experiment_tag`` that isn't a safe bare identifier.

    ``FileStorage`` builds filesystem paths as ``root / experiment_tag``;
    an unvalidated tag containing ``..``/path separators/an absolute
    path escapes ``root`` entirely (audit: security Finding 1, Critical
    — proven arbitrary write + arbitrary recursive delete via
    ``delete_experiment``). Called by every storage backend that
    consumes a caller-supplied tag, not just FileStorage, so the
    constraint is enforced uniformly regardless of backend.
    """
    if not _EXPERIMENT_TAG_RE.fullmatch(tag):
        raise ValueError(
            f"experiment_tag must match {_EXPERIMENT_TAG_RE.pattern!r} " f"(no path separators, '..', or absolute paths) — got {tag!r}",
        )
    return tag

CompositeHash = NewType("CompositeHash", str)
"""SHA256 of the concatenated system + user prompt text. The PK component
that makes resume cache deterministic across runs. Same prompts, same model,
same thinking-token → same hash → cache hit."""


# ──────────────────────────────────────────────────────────────────────
# Task abstraction
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TaskUnit:
    """One unit of work being benchmarked.

    Examples:
        - VocabApp: a synset (id="oewn-00791110-v", metadata={"pos": "v",
          "definition": "...", "senses": [...]})
        - JobApp: a job posting (id="job_abc123", metadata={"industry":
          "saas", "company": "...", "description": "..."})

    The framework treats ``id`` as opaque (used for ranking aggregation,
    DB row tagging, and substrate-chain lookup). ``metadata`` rides along
    so prompt builders can read whatever they need without the framework
    knowing the schema.

    ``stratum`` is the optional grouping key used for stratified sampling
    (VocabApp: POS = v/n/a/r; JobApp: industry vertical). When set, the
    framework records it on every row so downstream analytics can split
    by stratum. None = no stratification.
    """
    id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    stratum: str | None = None


# ──────────────────────────────────────────────────────────────────────
# Stage abstraction
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Stage:
    """One step of the per-task-unit pipeline.

    VocabApp example: enrich → validate_enrich + validate_examples_en
    + translate_<lang> → validate_translate_<lang>.

    ``op`` is the canonical short label (e.g. "enrich", "translate") used
    for budget bucketing and DB grouping. Multiple ``Stage`` objects can
    share the same ``op`` (e.g. translate_es and translate_de both have
    op="translate" so they share one budget cap).

    ``parent_stage`` defines the substrate chain: this stage's prompt
    builder sees the parent stage's parsed output via the StageContext.
    None = a root stage (only enrich-style producers have no parent).

    ``is_validator`` flags stages that audit a prior stage's output. When
    a candidate pool is supplied, the framework swaps in a different
    candidate as the validator (Layer-1 hard cross-family filter) so a
    model never validates its own output.
    """
    id: str
    op: str
    prompt_builder: Callable[..., Any]  # PromptBuilder Protocol — see stage.base
    parser: Callable[..., Any]  # ResponseParser Protocol — see stage.base
    parent_stage: str | None = None
    is_validator: bool = False
    lang: str | None = None
    budget_per_call: float = 0.05
    """Soft per-call cost projection used by the budget gate. The gate
    checks ``stage_total_spent + budget_per_call > stage_cap`` before
    each call; over-cap triggers SKIP, not crash."""


@dataclass
class StageGraph:
    """Topologically-ordered stage list. Rejects cycles + missing parents
    at construction time.

    The order in which stages run for a single (model, task_unit) pair
    is the topo order of this graph; sibling stages with the same parent
    can fire in parallel within one pipeline (per-stage cap permitting).
    """
    stages: list[Stage]

    def __post_init__(self) -> None:
        ids = {s.id for s in self.stages}
        if len(ids) != len(self.stages):
            raise ValueError("StageGraph: duplicate stage ids")
        for s in self.stages:
            if s.parent_stage is not None and s.parent_stage not in ids:
                raise ValueError(f"Stage {s.id!r} parent {s.parent_stage!r} not in graph")
        # Cycle detection via DFS — should never trigger given the
        # parent-stage-must-exist check above (cycles imply forward
        # references), but keep the assert for defence-in-depth.
        order = self.topo_order()
        if len(order) != len(self.stages):
            raise ValueError("StageGraph: cycle detected")

    def topo_order(self) -> list[Stage]:
        """Return stages in dependency order (parents before children)."""
        by_id = {s.id: s for s in self.stages}
        out: list[Stage] = []
        seen: set[str] = set()
        def visit(sid: str, stack: tuple[str, ...]) -> None:
            if sid in seen:
                return
            if sid in stack:
                raise ValueError(f"Cycle through {sid!r}: {(*stack, sid)}")
            s = by_id[sid]
            if s.parent_stage is not None:
                visit(s.parent_stage, (*stack, sid))
            seen.add(sid)
            out.append(s)
        for s in self.stages:
            visit(s.id, ())
        return out

    def by_id(self, sid: str) -> Stage:
        for s in self.stages:
            if s.id == sid:
                return s
        raise KeyError(sid)


# ──────────────────────────────────────────────────────────────────────
# Storage row + cache types
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CachedResponse:
    """In-memory snapshot of a successful prior call.

    Returned by ``BenchmarkStorage.get_cached`` / populated by
    ``prefetch_resume_cache``. The runner consults this BEFORE issuing an
    LLM call; a hit means zero spend.

    Failed rows (``error_class`` non-None and non-empty) are EXCLUDED
    from the cache — they get re-tried on next run.
    """
    composite_hash: str
    response: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    thinking: str
    source_tag: str | None = None  # informational; cache itself is tag-agnostic


@dataclass
class RunRow:
    """One persisted benchmark call.

    Identity: ``(composite_hash, provider, model, thinking)`` — the PK
    enforced by every storage backend. Re-recording the same key is a
    no-op (idempotent INSERT/upsert).

    Phase-4 OpenRouter fields (``cache_discount_usd``, ``is_byok``,
    ``web_search_citations``, ``upstream_resolved_model``) are nullable
    since non-OR providers don't expose them — readers must use ``None``
    semantics, not assume presence.
    """
    # Identity
    composite_hash: str
    provider: str
    model: str
    thinking: str
    # Routing / context
    stage: str
    task_unit_id: str
    stratum: str | None
    lang: str | None
    parent_prompt_hash: str | None
    experiment_tag: str
    # Outcome
    response: str | None
    error_class: str | None
    error_message: str | None
    # Telemetry
    cost_usd: float | None
    effective_cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cache_hit_tokens: int | None
    cache_write_tokens: int | None
    # Phase-4 OR extras (nullable; OR-only)
    cache_discount_usd: float | None = None
    is_byok: bool | None = None
    web_search_citations: list[dict[str, Any]] | None = None
    upstream_resolved_model: str | None = None
    # Provenance
    upstream_provider: str | None = None
    upstream_model: str | None = None
    native_finish_reason: str | None = None
    generation_id: str | None = None
    # Performance
    mean_attempt_duration_sec: float | None = None
    n_attempts: int | None = None
    http_status_sequence: list[int] | None = None
    """Per-attempt HTTP status codes across the underlying provider's own
    retry attempts for this call. NOT currently populated by
    round_runner.py — pyutilz's LLMProvider doesn't expose a per-attempt
    status history through the ``last_*`` telemetry attributes this
    framework reads (only the aggregate outcome). Full DDL/JSONL
    round-trip support exists so a future pyutilz version (or a custom
    ``provider_factory``) that DOES expose this can start populating it
    without a schema change; until then, always ``None``."""
    per_attempt_durations_sec: list[float] | None = None
    """Companion to ``http_status_sequence`` — same "not currently
    populated, wired for when the data becomes available" status."""
    # Logs / parser warnings
    logs: str | None = None
    """Free-form diagnostic text for this call. NOT currently populated
    — no code path in this framework writes to it today; reserved for
    a future structured-logging integration."""
    logs_compressed: bytes | None = None
    """Compressed form of ``logs`` for large payloads. Same "not
    currently populated" status."""
    parse_failure_prefix: str | None = None
    # Open-ended JSONB
    generation_details: dict[str, Any] = field(default_factory=dict)
    # Timestamp (storage backend fills this)
    ts: datetime | None = None

    @property
    def cache_key(self) -> tuple[str, str, str, str]:
        """This row's ``(composite_hash, provider, model, thinking)`` identity tuple.

        2026-08-02 near-duplicate-function-body finding: several storage-backend
        methods independently duplicated this tuple construction; formalizing it
        as one place (this property plus ``make_cache_key`` below for callers
        that only have the raw fields, not a ``RunRow`` instance) removes the
        risk of a future call site getting the field order wrong.
        """
        return make_cache_key(self.composite_hash, self.provider, self.model, self.thinking)


def make_cache_key(composite_hash: str, provider: str, model: str, thinking: str = "") -> tuple[str, str, str, str]:
    """The ``(composite_hash, provider, model, thinking)`` cache/PK identity tuple every
    storage backend keys on -- see ``RunRow.cache_key`` for the row-object counterpart."""
    return (composite_hash, provider, model, thinking)


# ──────────────────────────────────────────────────────────────────────
# Per-stage winners (round-to-round chaining)
# ──────────────────────────────────────────────────────────────────────

@dataclass
class WinnerSet:
    """Per-stage winners promoted from one Halving round to the next.

    ``winners`` maps stage label -> model id of the round-N winner. Round
    N+1 uses these as the substrate (e.g. validate_enrich in round N+1
    audits enrich produced by round-N's enrich-stage winner, not the
    candidate's own enrich output) — Layer-2 cross-family preference.

    ``stage_scores`` retains the winning score per stage so the
    post-promotion report can show the margin between winner and
    runner-up. ``runner_up`` records the second-place model per stage
    so the Phase-16 confirmation gate can compare them via Wilcoxon.

    Persisted by ``BenchmarkStorage.persist_winners`` and read back by
    ``load_winners`` between rounds.
    """
    experiment_tag: str
    round_idx: int
    winners: dict[str, str]  # stage_label -> model_id
    candidates_out: list[str] = field(default_factory=list)
    """The set of models surviving to the next round (after halving)."""

    stage_scores: dict[str, float] = field(default_factory=dict)
    """Stage label -> winning model's score. Empty when select_per_stage_winners
    skipped a stage (zero-signal)."""

    runner_up: dict[str, str | None] = field(default_factory=dict)
    """Stage label -> second-place model id (or None when only one
    candidate scored). Used by Phase 16 confirmation."""

    def get(self, stage: str) -> str | None:
        """Convenience accessor: stage label -> winning model id."""
        return self.winners.get(stage)
