"""cli/main.py tests — the entry point declared by pyproject.toml's
``[project.scripts]`` (``llm-bench = "llm_bench.cli.main:main"``) but,
until this fix, backed by no ``main.py`` at all: a fresh ``pip install``
+ ``llm-bench`` invocation crashed with ``ModuleNotFoundError`` (audit:
09-High). Covers ``--version``, ``doctor`` (env-var gated), and ``run``
(factory loading + a full in-memory Benchmark execution end-to-end).
"""

from __future__ import annotations

import sys
import types

import pytest

from llm_bench.cli.main import main

# ──────────────────────────────────────────────────────────────────────
# --version / help
# ──────────────────────────────────────────────────────────────────────

class TestVersionAndHelp:
    def test_version_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "llm-bench" in capsys.readouterr().out

    def test_no_command_prints_help_and_exits_zero(self, capsys):
        code = main([])
        assert code == 0
        assert "usage" in capsys.readouterr().out.lower()


# ──────────────────────────────────────────────────────────────────────
# doctor
# ──────────────────────────────────────────────────────────────────────

class TestDoctor:
    def test_warns_when_no_api_key_present(self, monkeypatch, capsys):
        for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_BENCH_DB_URL"):
            monkeypatch.delenv(name, raising=False)
        code = main(["doctor"])
        assert code == 1
        assert "WARN" in capsys.readouterr().out

    def test_ok_when_api_key_present(self, monkeypatch, capsys):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        monkeypatch.delenv("LLM_BENCH_DB_URL", raising=False)
        code = main(["doctor"])
        assert code == 0
        assert "OK" in capsys.readouterr().out

    def test_postgres_unreachable_reported_as_fail(self, monkeypatch, capsys):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_BENCH_DB_URL", "postgresql://nouser:nopass@127.0.0.1:1/doesnotexist")
        code = main(["doctor"])
        assert code == 1
        assert "FAIL" in capsys.readouterr().out

    def test_postgres_unreachable_does_not_leak_password_to_stdout(self, monkeypatch, capsys):
        # Regression test for audit: security 07-Low — a raw connection
        # failure must never echo the DSN's credentials to a consumer
        # that prints the caught exception verbatim, which `doctor`
        # does (see PostgresStorage.initialize()'s DSN redaction).
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_BENCH_DB_URL", "postgresql://nouser:supersecretpassword@127.0.0.1:1/doesnotexist")
        code = main(["doctor"])
        assert code == 1
        out = capsys.readouterr().out
        assert "supersecretpassword" not in out
        assert "nouser" not in out


# ──────────────────────────────────────────────────────────────────────
# run — factory loading + end-to-end in-memory execution
# ──────────────────────────────────────────────────────────────────────

def _install_fake_benchmark_module(monkeypatch) -> None:
    """Registers a throwaway module in sys.modules exposing a
    build_benchmark() factory, exercising --factory's 'module:attr'
    resolution without writing a file to the package tree."""
    from llm_bench import Benchmark, HalvingSchedule, InMemoryStorage, Stage, StageGraph, TaskUnit

    mod = types.ModuleType("_cli_test_fake_bench_module")

    class _FakePool:
        def __init__(self):
            self.units = [TaskUnit(id=f"u{i}", stratum="v") for i in range(2)]

        def sample(self, n):
            return self.units[:n]

        def total_size(self):
            return len(self.units)

    class _FakeProvider:
        def __init__(self, model):
            self.model = model
            self.last_cost_usd = 0.001
            self.last_effective_cost_usd = 0.001
            self.last_input_tokens = 10
            self.last_output_tokens = 5
            self.last_reasoning_tokens = 0

        async def generate(self, *, prompt, system):
            return '{"ok": true, "padding": "enough length here to clear scorer"}'

    def _pb(*, task_unit, ctx, lang=None):
        return ("sys", f"user {task_unit.id}")

    def _parser(*, task_unit, response_text, input_tokens, output_tokens):
        return {"raw": response_text} if response_text else None

    def build_benchmark():
        return Benchmark(
            task_pool=_FakePool(),
            stages=StageGraph([Stage(id="enrich", op="enrich", prompt_builder=_pb, parser=_parser)]),
            storage=InMemoryStorage(),
            halving_schedule=HalvingSchedule(round_sizes=(2,), units_per_arm=(1,), pool_size=2),
            provider_factory=lambda m: _FakeProvider(model=m),
        )

    setattr(mod, "build_benchmark", build_benchmark)  # noqa: B010 -- dynamic module attr by design
    monkeypatch.setitem(sys.modules, "_cli_test_fake_bench_module", mod)


class TestRun:
    def test_run_end_to_end_with_fake_factory(self, monkeypatch, capsys):
        _install_fake_benchmark_module(monkeypatch)
        code = main([
            "run",
            "--factory", "_cli_test_fake_bench_module:build_benchmark",
            "--tag", "cli_test_tag",
            "--candidates", "a,b",
        ])
        assert code == 0
        out = capsys.readouterr().out
        assert "experiment_tag=cli_test_tag" in out
        assert "final_candidates=" in out

    def test_run_rejects_empty_candidates(self, monkeypatch, capsys):
        _install_fake_benchmark_module(monkeypatch)
        code = main([
            "run",
            "--factory", "_cli_test_fake_bench_module:build_benchmark",
            "--tag", "cli_test_tag",
            "--candidates", " , ",
        ])
        assert code == 2

    def test_run_bad_factory_spec_raises(self, monkeypatch):
        with pytest.raises(ValueError, match=r"module\.path:attr"):
            from llm_bench.cli.main import _load_factory
            _load_factory("not_a_valid_spec")

    def test_run_missing_attr_raises(self, monkeypatch):
        _install_fake_benchmark_module(monkeypatch)
        from llm_bench.cli.main import _load_factory
        with pytest.raises(ValueError, match="no attribute"):
            _load_factory("_cli_test_fake_bench_module:does_not_exist")
