"""Runner subpackage — pipeline driver + Benchmark facade.

Public surface:
  - ``Benchmark`` — top-level orchestrator (composes pool + stages +
    storage + halving + scoring; consumers instantiate this once)
  - ``PhaseReport`` — return value of ``Benchmark.run_phase``
  - ``RoundConfig`` / ``run_round`` — single-round driver (use directly
    when the multi-round cascade isn't needed, e.g. tests)
  - ``ResumeCache`` — in-memory wrapper over ``BenchmarkStorage`` resume
  - ``BudgetGate`` / ``StageBudget`` — per-op spend cap
  - ``classify_provider_error`` — exception → stable label
"""

from llm_bench.runner.benchmark import Benchmark, PhaseReport, PreflightResult
from llm_bench.runner.budget import BudgetGate, StageBudget
from llm_bench.runner.classify import classify_provider_error
from llm_bench.runner.resume import ResumeCache
from llm_bench.runner.round_runner import RoundConfig, run_round

__all__ = [
    "Benchmark",
    "PhaseReport",
    "PreflightResult",
    "RoundConfig",
    "run_round",
    "ResumeCache",
    "BudgetGate",
    "StageBudget",
    "classify_provider_error",
]
