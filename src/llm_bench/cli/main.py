"""``llm-bench`` console-script entry point.

The framework is a library — consumers wire their own ``TaskPool``,
``StageGraph``, and prompt/parser functions in Python (see
``examples/job_app_cover_letter/run.py``) and call ``Benchmark`` directly.
This CLI does not hardcode any domain logic; it offers three things a
generic wrapper CAN do without knowing a consumer's task shape:

  - ``--version`` / ``doctor``: environment readiness checks.
  - ``run``: load a consumer-supplied zero-argument factory
    (``module:attr``, in the ``gunicorn``/``uvicorn`` style) that
    returns a ready-to-use ``Benchmark`` instance, then drive
    ``run_phase()`` with CLI-supplied tag/candidates/rounds.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import os
import sys
from typing import Any

from llm_bench import __version__


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """Check environment readiness: provider API keys, optional Postgres reachability."""
    ok = True

    key_names = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    present = [name for name in key_names if os.environ.get(name)]
    if present:
        print(f"[OK] provider API key(s) present: {', '.join(present)}")
    else:
        print(f"[WARN] no provider API key set (checked {', '.join(key_names)})")
        ok = False

    db_url = os.environ.get("LLM_BENCH_DB_URL")
    if db_url:
        try:
            asyncio.run(_check_postgres(db_url))
        except Exception as exc:
            print(f"[FAIL] Postgres unreachable at LLM_BENCH_DB_URL: {exc}")
            ok = False
        else:
            print("[OK] Postgres reachable at LLM_BENCH_DB_URL")
    else:
        print("[INFO] LLM_BENCH_DB_URL not set - Postgres storage not configured (file/in-memory backends still available)")

    return 0 if ok else 1


async def _check_postgres(url: str) -> None:
    from llm_bench.storage.postgres import PostgresStorage

    storage = PostgresStorage(url)
    await storage.initialize()
    await storage.close()


def _load_factory(spec: str) -> Any:
    """Resolve ``module.path:attr`` to the attribute, gunicorn/uvicorn-style."""
    if ":" not in spec:
        raise ValueError(f"--factory must be in 'module.path:attr' form, got {spec!r}")
    module_path, _, attr = spec.partition(":")
    module = importlib.import_module(module_path)
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise ValueError(f"module {module_path!r} has no attribute {attr!r}") from exc


async def _run_benchmark(args: argparse.Namespace) -> int:
    factory = _load_factory(args.factory)
    bench = factory()
    if inspect.isawaitable(bench):
        bench = await bench

    candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    if not candidates:
        print("error: --candidates must list at least one model id", file=sys.stderr)
        return 2
    rounds = None
    if args.rounds:
        rounds = [int(r.strip()) for r in args.rounds.split(",") if r.strip()]

    async with bench:
        report = await bench.run_phase(
            tag=args.tag, candidates=candidates, rounds=rounds, preflight=args.preflight,
        )

    print(f"experiment_tag={report.experiment_tag} rounds_run={len(report.rounds)} " f"elapsed_sec={report.elapsed_sec:.1f}")
    for rr in report.rounds:
        print(f"  round {rr.round_idx}: {len(rr.candidates_in)} in -> " f"{len(rr.candidates_out)} out ({len(rr.eliminated)} eliminated)")
    print(f"final_candidates={report.final_candidates}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    return asyncio.run(_run_benchmark(args))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-bench", description="Pluggable LLM benchmarking framework CLI.")
    parser.add_argument("--version", action="version", version=f"llm-bench {__version__}")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="Check environment readiness (provider API keys, optional Postgres reachability)")
    doctor.set_defaults(func=_cmd_doctor)

    run = sub.add_parser(
        "run",
        help="Run a benchmark phase using a consumer-supplied Benchmark factory",
        description="Loads a zero-argument factory ('module.path:attr', gunicorn/uvicorn-style) " "that returns a ready-to-use Benchmark instance, then drives run_phase().",
    )
    run.add_argument("--factory", required=True, help="module.path:attr resolving to a () -> Benchmark callable")
    run.add_argument("--tag", required=True, help="experiment_tag for this run")
    run.add_argument("--candidates", required=True, help="comma-separated model ids, e.g. 'openai/gpt-4o-mini,anthropic/claude-3-5-haiku'")
    run.add_argument("--rounds", default=None, help="comma-separated 1-indexed round numbers (default: all rounds in the schedule)")
    run.add_argument("--preflight", action="store_true", help="ping every candidate before round 1 and drop the dead ones")
    run.set_defaults(func=_cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
