"""JobApp Cover Letter benchmark - end-to-end POC.

Demonstrates the framework's no-DB / no-gold path:
  - JobPool reads jobs.csv (5 stratified jobs)
  - 4-stage pipeline (research -> draft -> polish + validate_draft)
  - FileStorage (JSONL + sqlite resume index, no Postgres needed)
  - gold_checker=None - ranking emerges from validator-pair scoring
  - Sequential Halving over 3 rounds: (3 candidates, 2, 1) sizes

Usage:
    python examples/job_app_cover_letter/run.py
        # uses default 3 candidates, file storage at ./benchmark_runs

    OPENROUTER_API_KEY=sk-or-... python examples/job_app_cover_letter/run.py \\
        --jobs 5 --candidates 3

    python examples/job_app_cover_letter/run.py --dry-run
        # uses a deterministic FakeProvider, no API call

Outputs the per-stage final ranking + total spend at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# Make the example self-contained when run from the repo root.
EXAMPLE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EXAMPLE_DIR))

from llm_bench import (  # noqa: E402
    Benchmark,
    FileStorage,
    HalvingSchedule,
)

from job_pool import JobPool  # noqa: E402
from stages import build_job_app_stage_graph, job_app_row_scorer  # noqa: E402

logger = logging.getLogger("job_app_bench")


DEFAULT_CANDIDATES = [
    "google/gemini-2.5-flash-lite",
    "qwen/qwen3-30b-a3b-instruct-2507",
    "openai/gpt-4.1-mini",
]


# ──────────────────────────────────────────────────────────────────────
# Dry-run fake provider
# ──────────────────────────────────────────────────────────────────────

class _FakeProvider:
    """Deterministic stand-in for live LLMs - returns plausible JSON."""

    def __init__(self, model: str):
        self.model = model
        self.last_input_tokens = 200
        self.last_output_tokens = 150
        self.last_reasoning_tokens = 0
        self.last_cost_usd = 0.001 if "lite" in model else 0.003
        self.last_effective_cost_usd = self.last_cost_usd

    async def generate(self, *, prompt: str, system: str) -> str:
        # Cheap deterministic outputs that pass each parser. Order
        # matters: validator check FIRST since the validator prompt
        # also mentions "cover letter".
        sys_lc = system.lower()
        if "auditing" in sys_lc or "grade it" in sys_lc:
            # Validator-independence (llm_bench wires a DIFFERENT
            # candidate to answer an is_validator stage than the one
            # being judged) means `self.model` here is the VALIDATOR,
            # which may not be the producer whose draft this is. Grade
            # what's actually being judged — the draft CONTENT in
            # `prompt` — via the "[by:<producer_model>]" marker the
            # draft-writing branch below stamps into the letter text,
            # not the validator's own identity. Reading `self.model`
            # here (the original version of this fixture) only ever
            # produced a meaningful signal by accident, back when the
            # framework let a model self-validate and producer ==
            # validator was always true.
            quality = 0.78 if "[by:" in prompt and "lite" in prompt.split("[by:", 1)[1].split("]", 1)[0] else 0.92
            return (
                f'{{"intent_match": {quality:.2f},'
                f'"skill_coverage": {quality - 0.05:.2f},'
                f'"tone": {quality:.2f},'
                f'"reasoning": "addresses the buyer signals well"}}'
            )
        if "extract the buyer" in sys_lc or "decoding job_app" in sys_lc:
            return (
                '{"intent": "ship a production system fast",'
                '"buyer_signals": ["urgency", "production focus"],'
                '"risk_flags": ["scope ambiguity"],'
                '"skill_gaps": []}'
            )
        if "polish the supplied draft" in sys_lc or "cover-letter editor" in sys_lc:
            return (
                '{"letter": "Hi, I have shipped 12+ production systems on '
                "this exact stack. Happy to share case studies on a quick "
                'call.",'
                '"edits_made": ["tightened opening", "removed filler"]}'
            )
        if "writing a cover letter" in sys_lc or "freelance pitch" in sys_lc:
            # The "[by:<model>]" marker lets the validator branch above
            # grade the actual producer instead of whichever model
            # happens to be answering the validate_draft call.
            return (
                f'{{"letter": "[by:{self.model}] I have shipped 12+ '
                "production systems on the exact stack you describe. I "
                "ship clean async code, ship fast, and stay in scope. "
                'Happy to share two relevant case studies on a 15-min call.",'
                '"key_skills_addressed": ["python", "fastapi", "async"]}'
            )
        return '{"ok": true, "padding": "placeholder response with enough length"}'


def _factory_dry_run(model: str) -> _FakeProvider:
    return _FakeProvider(model)


def _factory_live(model: str):
    from pyutilz.llm import get_llm_provider
    return get_llm_provider("openrouter", model=model)


# ──────────────────────────────────────────────────────────────────────
# Driver
# ──────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    pool = JobPool(jobs_csv=args.jobs_csv)
    pool_total = pool.total_size()
    if pool_total == 0:
        logger.error("[job_app] empty pool at %s", args.jobs_csv)
        return 1

    storage_root = Path(args.storage_root)
    storage = FileStorage(storage_root)
    stages = build_job_app_stage_graph()

    n_jobs = min(args.jobs, pool_total)
    schedule = HalvingSchedule(
        round_sizes=(min(args.candidates, 3), max(args.candidates // 2, 1)),
        units_per_arm=(min(2, n_jobs), n_jobs),
        pool_size=n_jobs,
    )

    candidates = args.candidates_list or DEFAULT_CANDIDATES[: args.candidates]
    if args.dry_run:
        provider_factory = _factory_dry_run
    else:
        if not os.environ.get("OPENROUTER_API_KEY"):
            logger.error("[job_app] OPENROUTER_API_KEY not set; pass --dry-run for " "deterministic offline mode")
            return 2
        provider_factory = _factory_live

    bench = Benchmark(
        task_pool=pool,
        stages=stages,
        storage=storage,
        halving_schedule=schedule,
        gold_checker=None,
        row_scorer=job_app_row_scorer,
        provider_factory=provider_factory,
        global_concurrency=args.concurrency,
    )

    tag = f"job_app_cl_{int(time.time())}"
    logger.info(
        "[job_app] tag=%s candidates=%d jobs=%d schedule=%s storage=%s",
        tag, len(candidates), n_jobs, schedule, storage_root,
    )
    async with bench:
        report = await bench.run_phase(tag=tag, candidates=candidates)

    print("\n" + "=" * 70)
    print(f"JobApp benchmark complete: tag={tag}")
    print(f"Elapsed: {report.elapsed_sec:.1f}s")
    print(f"Final survivors: {report.final_candidates}")
    print(f"\nPer-stage rankings:")
    if report.final_ranking is not None:
        for stage, sr in sorted(report.final_ranking.stages.items()):
            print(f"  [{stage}] winner: {sr.winner}")
            for model, score in sorted(
                sr.model_scores.items(), key=lambda kv: -kv[1],
            ):
                eff = sr.model_eff_cost.get(model, 0.0)
                print(f"      {model:<55s} score={score:.3f} eff_cost=${eff:.4f}")
    print(f"\nResults persisted at: {storage_root.absolute()}")
    print("=" * 70)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--jobs-csv", default=str(EXAMPLE_DIR / "data" / "jobs.csv"),
    )
    p.add_argument("--jobs", type=int, default=5)
    p.add_argument("--candidates", type=int, default=3)
    p.add_argument(
        "--candidates-list", nargs="*", default=None,
        help="Explicit list of OR model ids; overrides --candidates",
    )
    p.add_argument("--storage-root", default="./benchmark_runs")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument(
        "--dry-run", action="store_true",
        help="Use deterministic FakeProvider (no API key, no spend)",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
