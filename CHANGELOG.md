# Changelog

All notable changes to `llm_bench` will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- Repo skeleton extracted from VocabApp's benchmark framework, built out in
  phases: storage Protocol + `InMemoryStorage` (Phase A), Sequential
  Halving + cost ranker + per-stage winners (Phase B), the row/gold
  scoring layer (Phase C), `FileStorage` (JSONL + SQLite resume index) and
  `PostgresStorage` (asyncpg) (Phase D), the `Benchmark` facade +
  `round_runner` + `ResumeCache` + `BudgetGate` + `classify_provider_error`
  (Phase E), a `Benchmark.preflight()` pre-round dead-model check, and a
  reference consumer example (JobApp cover-letter POC: `FileStorage`, no
  `GoldChecker`, validator-pair ranking) (Phase G).
- Package layout: `core/`, `pool/`, `stage/`, `halving/`, `ranking/`, `cost/`,
  `discovery/`, `storage/`, `runner/`, `confirmation/`, `cli/`.
- `docs/architecture.md` — module map + `run_phase()` data-flow walkthrough.
- `llm-bench` CLI (`cli/main.py`): `--version`, `doctor` (provider-key /
  Postgres-reachability check), `run --factory module:attr` (loads a
  consumer-supplied `Benchmark` factory and drives `run_phase()`).
- Test suite: storage-backend contract tests (parametrized over
  InMemory/File/Postgres, the last gated on `LLM_BENCH_TEST_DB_URL`),
  Sequential Halving unit + property-based (hypothesis) tests,
  `round_runner`/`BudgetGate`/`ResumeCache`/OpenRouter-catalogue/error-
  classification unit tests, an end-to-end in-memory smoke suite, and the
  `tests/test_meta/` framework-hygiene gate (API-stability snapshot,
  `pyutilz.dev.code_audit` static-analysis baseline, import-cycle /
  bare-except / lazy-logging / no-unicode-console checks).
- CI/tooling hardening: mypy + Black gates via shared `py-ci-shared`
  workflows (badged in README), `ruff-base.toml` shared select set,
  bandit/interrogate/deptry/codespell config, `pyutilz.dev.code_audit`
  baseline scanner wired into the meta-test gate, GitHub Actions SHA-pinned
  against zizmor findings (`persist-credentials: false`, explicit
  `permissions:` blocks), `.pre-commit-config.yaml` brought to parity with
  the shared mlframe/pyutilz hook set, `uv`-based dependency install +
  build-smoke + release workflow.
- Bootstrap files copied/adapted from `pyutilz`: `pyproject.toml`
  (ruff/black/mypy/coverage/pytest config), `.github/workflows/ci.yml`,
  `.pre-commit-config.yaml` (detect-secrets + meta-tests), `.env.example`,
  `LICENSE` (MIT).

### Fixed
Full-repository audit (`audits/2026-07-22_full-audit/`, 162 findings
across code quality, architecture, tests, performance, edge cases, and
SQL/HTTP/security/API/LLM best practices) implemented in full, including
every Low/Info-severity item. Highlights:
- **Security**: `FileStorage` path-traversal via unvalidated
  `experiment_tag` (arbitrary write + arbitrary recursive delete through
  `delete_experiment`'s `shutil.rmtree`); `PostgresStorage` schema-name
  SQL-identifier validation.
- **Correctness**: resume-cache retry semantics (`record_call` was
  either always overwriting or never overwriting a prior failed row
  depending on backend — now uniformly "a failed row is overwritable by
  a later success, a successful row never is" across all three
  backends); `prefetch_resume_cache` crashing on real Postgres
  (`NoActiveSQLTransactionError`, missing explicit transaction);
  `composite_hash` NUL-byte boundary collision (length-prefixed now,
  found by the new hypothesis property suite); validator independence
  (a candidate could end up validating its own output); winner-substrate
  promotion not actually wired into round-to-round prompt building;
  final-round specialist-preservation over-promoting past the schedule's
  declared single-winner contract.
- **Performance**: `mad_bootstrap_prune`'s paired- and unpaired-bootstrap
  branches were O(n²·n_bootstrap) (redundant per-candidate resample
  regeneration) — hoisted to O(n·n_bootstrap); `BudgetGate` re-aggregated
  full spend from storage on every `check()` call — now seeded once per
  `(tag, op)`; a budget-gate TOCTOU race allowing concurrent calls to
  collectively overshoot a cap.
- **Packaging/docs**: `llm-bench` console script pointed at a nonexistent
  `cli/main.py` (crashed with `ModuleNotFoundError` on a fresh install);
  README quick-start example had two `TypeError`-inducing mistakes
  (missing required `Stage.op`, `candidates` passed to `Benchmark()`
  instead of `run_phase()`); `docs/architecture.md` was a dead link.

### Notes
- Depends on LLMProvider, OpenRouterProvider, `list_openrouter_models()`
  (8x speedup + two-stage health pre-flight), unified `reasoning`
  field, `supports_json_mode()`, and the Phase-4 OR extras — all land
  in `pyutilz` at/after `1.1`, but `pyproject.toml` pins the interim
  floor `pyutilz>=1.0` (see that file's own comment) until `1.1` is
  actually tagged; tighten the floor once it is, rather than bumping
  it preemptively to a version that doesn't exist yet.
