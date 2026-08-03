"""Smoke test for the JobApp example - exercises the no-DB / no-gold path.

The example is the consumer that proves the framework's "Postgres-free,
gold-free" mode actually works end-to-end: FileStorage on disk, no
GoldChecker, ranking emerges from validator-pair scoring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "job_app_cover_letter"
sys.path.insert(0, str(EXAMPLE_DIR))


@pytest.mark.asyncio
async def test_job_app_dry_run_completes(tmp_path):
    """End-to-end via Benchmark.run_phase + FileStorage + FakeProvider."""
    from llm_bench import Benchmark, FileStorage, HalvingSchedule

    from job_pool import JobPool
    from run import _factory_dry_run
    from stages import build_job_app_stage_graph, job_app_row_scorer

    storage = FileStorage(tmp_path / "runs")
    pool = JobPool(jobs_csv=EXAMPLE_DIR / "data" / "jobs.csv")
    schedule = HalvingSchedule(
        round_sizes=(3, 2), units_per_arm=(2, 3), pool_size=5,
    )
    bench = Benchmark(
        task_pool=pool,
        stages=build_job_app_stage_graph(),
        storage=storage,
        halving_schedule=schedule,
        gold_checker=None,
        row_scorer=job_app_row_scorer,
        provider_factory=_factory_dry_run,
        global_concurrency=4,
    )
    candidates = [
        "google/gemini-2.5-flash-lite",
        "qwen/qwen3-30b-a3b-instruct-2507",
        "openai/gpt-4.1-mini",
    ]
    async with bench:
        report = await bench.run_phase(tag="test_job_app", candidates=candidates)

    assert len(report.rounds) == 2
    assert report.final_ranking is not None
    # All 4 stages produced rankings.
    assert {"research", "draft", "polish", "validate_draft"}.issubset(
        report.final_ranking.stages,
    )
    # validate_draft scoring distinguished the lite model from the others
    # (FakeProvider returns 0.78 for lite, 0.92 for the others).
    vd = report.final_ranking.stages["validate_draft"].model_scores
    assert "google/gemini-2.5-flash-lite" in vd
    # The lite-model fake has the lowest validator score.
    sorted_models = sorted(vd.items(), key=lambda kv: kv[1])
    assert sorted_models[0][0] == "google/gemini-2.5-flash-lite"


def test_job_app_jobpool_loads_csv():
    """JobPool reads jobs.csv and returns the expected stratification."""
    from job_pool import JobPool

    pool = JobPool(jobs_csv=EXAMPLE_DIR / "data" / "jobs.csv")
    units = pool.sample(5)
    assert len(units) == 5
    industries = {u.stratum for u in units}
    # CSV ships 5 distinct industries
    assert industries == {"saas", "fintech", "ecommerce", "healthtech"}


def test_job_app_stage_graph_topology():
    """The 4-stage graph is well-formed (parents resolve, no cycles)."""
    from stages import build_job_app_stage_graph

    g = build_job_app_stage_graph()
    assert len(g.stages) == 4
    by_id = {s.id: s for s in g.stages}
    assert by_id["research"].parent_stage is None
    assert by_id["draft"].parent_stage == "research"
    assert by_id["polish"].parent_stage == "draft"
    assert by_id["validate_draft"].parent_stage == "draft"
    assert by_id["validate_draft"].is_validator is True
    # Topo order: research before draft, draft before polish + validate_draft
    order = [s.id for s in g.topo_order()]
    assert order.index("research") < order.index("draft")
    assert order.index("draft") < order.index("polish")
    assert order.index("draft") < order.index("validate_draft")
