# llm-bench

[![CI](https://github.com/fingoldo/llm_bench/workflows/CI/badge.svg)](https://github.com/fingoldo/llm_bench/actions/workflows/ci.yml)
[![MyPy](https://github.com/fingoldo/llm_bench/actions/workflows/mypy-full.yml/badge.svg)](https://github.com/fingoldo/llm_bench/actions/workflows/mypy-full.yml)
[![Black](https://github.com/fingoldo/llm_bench/workflows/Black/badge.svg)](https://github.com/fingoldo/llm_bench/actions/workflows/black-filtered.yml)
[![codecov](https://codecov.io/gh/fingoldo/llm_bench/branch/main/graph/badge.svg)](https://codecov.io/gh/fingoldo/llm_bench)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Pluggable LLM benchmarking framework: cost-rank discovery + Sequential Halving + per-stage winner promotion + gold-anchored ranking + cross-tag resume cache, with **three storage backends** (Postgres / file / in-memory).

## Why

Real LLM benchmarking (which model solves my task at lowest cost?) is heavyweight: 60–100 candidates × N task units × M stages × multiple rounds = thousands of paid LLM calls. You need:

- Cost-rank discovery (don't burn budget on $5/M-token frontier models when sub-$1/M cheap-pool will do)
- Sequential Halving (drop the bottom half each round — Phase 1 only spends on the candidates still in the running)
- Resume cache (an interrupted run resumes without re-paying for completed calls)
- Per-stage budget gates (each stage has its own $ cap; budget exhaustion skips, doesn't crash)
- Pluggable storage (Postgres for production, JSONL files for solo runs / no-DB envs, in-memory for tests)
- Validator-pairing for cross-family scoring when no gold-truth is available

This framework provides all of the above. Two reference consumers: **VocabApp** (synset enrichment + translation pipeline) and **JobApp Cover Letter Generation** (research → draft → polish → validate).

## Status

`0.1.0` — alpha. API is stabilizing. See [docs/architecture.md](docs/architecture.md).

## Install

```bash
pip install llm-bench[postgres]   # full install with PG storage
pip install llm-bench              # core only (in-memory + file storage)
pip install -e .[dev,postgres]    # development install
```

## Quick start

```python
from llm_bench import Benchmark, Stage, StageGraph, HalvingSchedule, CostFilter
from llm_bench.storage import FileStorage

bench = Benchmark(
    task_pool=my_pool,                           # implements TaskPool Protocol
    stages=StageGraph([
        Stage(id="draft", op="draft",
              prompt_builder=build_draft, parser=parse_draft,
              budget_per_call=0.50),
        Stage(id="validate_draft", op="validate_draft", parent_stage="draft", is_validator=True,
              prompt_builder=build_validate, parser=parse_validate,
              budget_per_call=0.10),
    ]),
    storage=FileStorage(root="./benchmark_runs"),
    cost_filter=CostFilter(max_output_price_per_m=2.0, min_context_length=64000),
    halving_schedule=HalvingSchedule(round_sizes=(20, 10, 5), units_per_arm=(3, 6, 10)),
)
report = await bench.run_phase(
    tag="exp_v1",
    candidates=["openai/gpt-4o-mini", "anthropic/claude-3-5-haiku", "google/gemini-flash-1.5"],
    rounds=[1, 2, 3],
)
await bench.aclose()
```

## Tests

```bash
pytest -m "not live"          # offline suite (default in CI)
pytest -m live                 # full suite including paid LLM calls
pytest tests/test_meta/        # framework hygiene meta-tests
```

## Environment variables

| Name | Description |
|---|---|
| `LLM_BENCH_DB_URL` | Postgres connection string for `PostgresStorage` (optional -- file/in-memory backends work without it; checked by `llm-bench doctor`). |
| `OPENROUTER_API_KEY` | OpenRouter provider API key (optional -- needed only when routing candidates through OpenRouter; checked by `llm-bench doctor`). |
| `ANTHROPIC_API_KEY` | Anthropic provider API key (optional -- needed only when routing candidates directly to Anthropic; checked by `llm-bench doctor`). |
| `OPENAI_API_KEY` | OpenAI provider API key (optional -- needed only when routing candidates directly to OpenAI; checked by `llm-bench doctor`). |

## Security notes

Results (full prompt/response text) are stored in **plaintext** across all three backends — `FileStorage`'s JSONL files and `PostgresStorage`'s tables have no built-in field-level encryption. At-rest encryption is the deployment's responsibility (Postgres TDE / an encrypted volume for `FileStorage`), not something this framework applies itself. Relevant if a consumer's task units carry sensitive data (e.g. real job postings, PII).

## License

[MIT](LICENSE)
