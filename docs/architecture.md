# Architecture

This document maps `llm_bench`'s modules to the concepts in the README
and explains how a `Benchmark.run_phase()` call flows through the
framework. See individual module docstrings for implementation detail
— this page is the orientation layer, not a spec.

## Design goals

- **Cost-bounded discovery.** A candidate pool of 20-100 models is
  narrowed via Sequential Halving so paid LLM spend concentrates on
  the models still in contention, not the ones already eliminated.
- **Resumability.** Any interrupted run picks back up without
  re-paying for calls already recorded, keyed on a deterministic hash
  of the exact prompt pair sent.
- **Pluggable everything.** Storage backend, task source, prompt
  building, response parsing, scoring, and gold-truth checking are all
  `Protocol`-typed extension points — the framework core has no
  hardcoded dependency on VocabApp's or JobApp's domain logic.
- **No self-judgment.** When a stage validates another stage's output,
  the validator is drawn from a *different* model family than the
  producer (Layer-1 hard rule) — a model's own output is never scored
  by that same model.

## Module map

```
core/       Stage/TaskUnit/RunRow/ExperimentTag types, composite_hash,
            cost_tiebreak_key, is_row_usable predicate
pool/       TaskPool Protocol (source of TaskUnits)
stage/      StageContext, PromptBuilder/ResponseParser/GoldChecker
            Protocols
provider/   LLMProvider Protocol (the .generate() call surface)
storage/    BenchmarkStorage Protocol + InMemory/File/Postgres backends
cost/       ModelCatalogue Protocol, OpenRouterCatalogue, CostFilter,
            cost estimation (estimate_call_cost / estimate_top_n_by_cost)
halving/    HalvingSchedule, Halving driver (promote()), MAD-bootstrap
            pruner, validator pairing, task-unit assignment, alive/dead
            error-class filter
ranking/    compute_ranking (per-stage RankingReport), per-stage winner
            selection, winner-substrate loading for round-to-round
            chaining
runner/     Benchmark (top-level orchestrator), round_runner
            (per-round pipeline execution), BudgetGate, ResumeCache,
            classify_provider_error
cli/        Entry point for the `llm-bench` console script
```

`discovery/` and `confirmation/` are currently empty reserved
subpackages — no functionality lives there yet.

## Data flow: one `run_phase()` call

```
Benchmark.run_phase(tag, candidates, rounds)
  │
  ├─ storage.initialize() + ResumeCache.populate_from_storage()   (once)
  │
  └─ for round_idx in rounds:
       round_runner.run_round(RoundConfig(candidates, task_units, stages, ...))
         │
         ├─ _preload_winner_substrates(cfg)      # load prior round's M_X winners
         │
         └─ for candidate × task_unit:            (bounded by global_concurrency
              _run_pipeline(...)                    and per_op_concurrency)
                │
                └─ for stage in StageGraph (topo order):
                     ├─ resolve validator model if stage.is_validator
                     │    (allowed_validator_pairs — cross-family only)
                     ├─ build prompt (PromptBuilder), hash it (composite_hash)
                     ├─ resume-cache lookup — hit: skip the call entirely
                     ├─ miss: BudgetGate.check() → call provider.generate()
                     │    → classify_provider_error() on failure
                     ├─ parse response (ResponseParser) → StageContext.outputs
                     └─ storage.record_call(RunRow)   # idempotent upsert
         │
         ├─ ranking.compute_ranking(storage rows)  → StageRanking per stage
         ├─ select_per_stage_winners(...)           → this round's M_X winners
         └─ Halving.promote(round_idx, scores, eff_cost, ...)
              ├─ coverage gate (drop < coverage_min stage-attempt rate)
              ├─ mad_bootstrap_prune (O1 statistical outlier removal)
              ├─ halving cut (top `next_round_size` by cost_tiebreak_key)
              ├─ specialist preservation (force-promote per-stage winners
              │    the aggregate cut eliminated — skipped on the final round)
              ├─ multi-specialty bonus (+0.05 for >=2 stage wins, carried
              │    into next round's aggregate score)
              └─ alive/dead filter (is_alive_candidate on error_history)
       │
       └─ RoundResult.candidates_out becomes next round's `candidates`
```

`PhaseReport` (the `run_phase()` return value) carries every round's
`RoundResult` plus the final `RankingReport`.

## Core concepts

**`TaskUnit`** — one unit of work a candidate is scored on (a VocabApp
synset, an JobApp job posting, ...). Domain-specific; the framework
only needs `id` and `stratum` for pool sampling / stratified rounds.

**`Stage` / `StageGraph`** — a DAG of pipeline steps. Each `Stage` has
a required `op` (budget/DB grouping label — multiple stages can share
one, e.g. `translate_es`/`translate_de` both bucket under `translate`)
and an optional `parent_stage` defining the substrate chain: a child
stage's `PromptBuilder` reads the parent's parsed output via
`StageContext.outputs`. `is_validator=True` stages get a
cross-family-swapped model instead of the producing candidate.

**`composite_hash(system_text, user_text)`** — SHA-256 over a
length-prefixed `(system, user)` pair (`core/hashing.py`). The
system/user split is part of the fingerprint (Anthropic prompt-cache
discounts apply to the system block, so two prompts with identical
concatenated text but a different split are NOT cache-equivalent on
the provider side). The resume-cache key is
`(composite_hash, provider, model, thinking)` — same prompt, different
route (model or thinking level), separate cache entry.

**`RunRow`** — one persisted (stage, model, task_unit) result.
`record_call` is idempotent on the PK above: a fresh insert for a new
key, an overwrite of a prior FAILURE by a later successful retry, and
— important for correctness — a successful row is never overwritten
by a later failure.

**`ExperimentTag`** — validated bare identifier
(`^[A-Za-z0-9_.-]{1,200}$`) used both as a storage-query partition key
and, for `FileStorage`, as a filesystem directory name — the
validation exists specifically to block path traversal through a
caller-supplied tag.

## Sequential Halving

`HalvingSchedule` is the plan-table: `round_sizes` (candidates
surviving into each round) and `units_per_arm` (task units assigned
per candidate per round), same length, monotonically non-increasing.
`Halving.promote(just_finished_round_idx, scores, eff_cost, ...)`
applies, in order: the coverage gate, `mad_bootstrap_prune` (O1
statistical prune — MAD-based lower-confidence-bound cutoff, with an
optional paired-bootstrap mode when per-task-unit score breakdowns and
effective cost are supplied), the halving cut (`cost_tiebreak_key`
sorts by score first, cheaper cost as tiebreak, then latency), per-
stage specialist preservation, the multi-specialty score bonus, and
finally the alive/dead filter (`is_alive_candidate` against this run's
accumulated `error_history`, distinguishing permanent-error classes
from transient/retryable ones).

## Storage backends

All three (`InMemoryStorage`, `FileStorage`, `PostgresStorage`)
implement the same `BenchmarkStorage` Protocol and pass the shared
contract suite in `tests/unit/test_storage_protocol.py`
(parametrized) plus a Postgres-specific variant in
`tests/integration/test_storage_protocol_postgres.py` (gated on
`LLM_BENCH_TEST_DB_URL`). `FileStorage` persists JSONL result/prompt
logs per experiment tag plus a SQLite index for the resume-cache
lookup; `PostgresStorage` is the production backend (asyncpg pool,
CHECK constraints on cost/token columns, a `schema_version` guard that
raises a clear error on a stale deployed schema instead of a confusing
mid-run `UndefinedColumnError`).

## Extension points

Consumers plug in by implementing these Protocols (see `stage/base.py`
and `pool/base.py`):

- `TaskPool` — `sample(n)` / `total_size()`, sync or async.
- `PromptBuilder` — `(task_unit, ctx, lang) -> (system_text, user_text)`.
- `ResponseParser` — `(task_unit, response_text, input_tokens,
  output_tokens) -> dict | None` (`None` signals a parse failure,
  cached separately from a successful result so it isn't silently
  retried forever nor silently treated as a resumable success).
- `GoldChecker` / `RowScorer` — optional; when absent,
  `default_row_scorer` (length/error-based) is used.
- `provider_factory` — `Benchmark`'s hook for swapping in a fake
  provider under test, or a non-default real provider in production
  (defaults to `pyutilz.llm.get_llm_provider`).

## Where to look next

- `runner/round_runner.py` — the per-round execution core (winner-
  substrate wiring, validator independence, circuit breaker, budget
  gate integration).
- `halving/driver.py` — the promotion state machine described above.
- `ranking/ranker.py` / `ranking/per_stage_winners.py` — scoring and
  per-stage winner selection feeding the next round.
- `tests/unit/test_round_runner.py` — the most direct executable
  documentation of round-to-round behavior (winner substrate reuse,
  coverage-gate interaction with cache hits, circuit breaker, etc).
