"""llm_bench — pluggable LLM benchmarking framework.

Public re-exports below define the API surface that consumers depend on.
Adding/removing entries here is an API change — covered by
``tests/test_meta/test_api_stability.py``.
"""

__version__ = "0.1.0"

# ── Core types ─────────────────────────────────────────────────────
from llm_bench.core.types import (
    TaskUnit,
    Stage,
    StageGraph,
    RunRow,
    CachedResponse,
    ExperimentTag,
    WinnerSet,
)

# ── Protocols ──────────────────────────────────────────────────────
from llm_bench.pool.base import TaskPool
from llm_bench.stage.base import PromptBuilder, ResponseParser, GoldChecker
from llm_bench.storage.base import BenchmarkStorage
from llm_bench.cost.catalogue import ModelCatalogue

# ── Storage backends ───────────────────────────────────────────────
from llm_bench.storage import FileStorage, InMemoryStorage, PostgresStorage

# ── Cost / discovery ───────────────────────────────────────────────
from llm_bench.cost.filter import CostFilter

# ── Halving / ranking ──────────────────────────────────────────────
from llm_bench.halving import (
    HalvingSchedule,
    Halving,
    RoundResult,
    mad_bootstrap_prune,
    assign_task_units_for_round,
    allowed_validator_pairs,
    select_validator_for_producer,
    is_alive_candidate,
    load_dead_model_ids,
    DEAD_ERROR_CLASSES,
    TRANSIENT_ERROR_CLASSES,
)
from llm_bench.core.scoring import cost_tiebreak_key

# ── Cost / discovery ───────────────────────────────────────────────
from llm_bench.cost import (
    CatalogueEntry,
    CostEstimate,
    CostProfile,
    OpenRouterCatalogue,
    estimate_call_cost,
    estimate_top_n_by_cost,
)

# ── Ranking ────────────────────────────────────────────────────────
from llm_bench.ranking import (
    RankingReport,
    StageRanking,
    RowScorer,
    compute_ranking,
    default_row_scorer,
    load_winner_substrate,
    select_per_stage_winners,
)

# ── Runner ─────────────────────────────────────────────────────────
from llm_bench.runner import (
    Benchmark,
    PhaseReport,
    PreflightResult,
    RoundConfig,
    ResumeCache,
    BudgetGate,
    StageBudget,
    classify_provider_error,
    run_round,
)

__all__ = [
    "__version__",
    # Types
    "TaskUnit", "Stage", "StageGraph", "RunRow", "CachedResponse",
    "ExperimentTag", "WinnerSet",
    # Protocols
    "TaskPool", "PromptBuilder", "ResponseParser", "GoldChecker",
    "BenchmarkStorage", "ModelCatalogue",
    # Backends
    "InMemoryStorage", "FileStorage", "PostgresStorage",
    # Cost
    "CostFilter",
    # Halving
    "HalvingSchedule", "Halving", "RoundResult",
    "mad_bootstrap_prune", "assign_task_units_for_round",
    "allowed_validator_pairs", "select_validator_for_producer",
    "is_alive_candidate", "load_dead_model_ids",
    "DEAD_ERROR_CLASSES", "TRANSIENT_ERROR_CLASSES",
    # Scoring helpers
    "cost_tiebreak_key",
    # Cost / discovery
    "CatalogueEntry", "CostEstimate", "CostProfile",
    "OpenRouterCatalogue",
    "estimate_call_cost", "estimate_top_n_by_cost",
    # Ranking
    "RankingReport", "StageRanking", "RowScorer",
    "compute_ranking", "default_row_scorer",
    "select_per_stage_winners", "load_winner_substrate",
    # Runner
    "Benchmark", "PhaseReport", "PreflightResult",
    "RoundConfig", "ResumeCache",
    "BudgetGate", "StageBudget",
    "classify_provider_error", "run_round",
]
