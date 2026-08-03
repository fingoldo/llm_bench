"""Register the meta-test baseline-refresh flags so pytest accepts them.

The snapshot-style meta-sensors (api-snapshot, code-audit) read
``sys.argv`` directly to decide whether to re-seed their baseline file,
but pytest rejects unknown command-line args before tests run, so the
flags must be declared here via ``pytest_addoption``.

Usage (re-seed a baseline after intentionally changing the tree):
    pytest tests/test_meta/ --refresh-code-audit-baseline
"""

from __future__ import annotations

from py_ci_shared.code_audit_meta import register_refresh_option as register_code_audit_refresh_option
from py_ci_shared.loc_budget import register_refresh_option as register_loc_budget_refresh_option
from py_ci_shared.readme_env_var_parity import register_refresh_option as register_readme_env_var_refresh_option


def pytest_addoption(parser):
    group = parser.getgroup("llm-bench-meta-baselines")
    group.addoption("--refresh-api-snapshot", action="store_true", default=False, help="Re-seed _api_snapshot.json.")
    group.addoption("--refresh-unwired-capabilities-baseline", action="store_true", default=False, help="Re-seed _unwired_capabilities_baseline.json.")
    group.addoption(
        "--refresh-duplicate-magic-literals-baseline",
        action="store_true",
        default=False,
        help="Re-seed _duplicate_magic_literals_baseline.json.",
    )
    group.addoption(
        "--refresh-protocol-async-dispatch-baseline",
        action="store_true",
        default=False,
        help="Re-seed _protocol_async_optional_dispatch_baseline.json.",
    )
    group.addoption(
        "--refresh-sql-conflict-baseline",
        action="store_true",
        default=False,
        help="Re-seed _sql_on_conflict_baseline.json.",
    )
    group.addoption(
        "--refresh-unvalidated-identifier-baseline",
        action="store_true",
        default=False,
        help="Re-seed _unvalidated_identifier_baseline.json.",
    )
    group.addoption(
        "--refresh-test-source-parity-baseline",
        action="store_true",
        default=False,
        help="Re-seed _test_source_parity_baseline.json (forward: untested modules).",
    )
    group.addoption(
        "--refresh-test-source-parity-reverse-baseline",
        action="store_true",
        default=False,
        help="Re-seed _test_source_parity_reverse_baseline.json (reverse: orphan test files).",
    )
    group.addoption(
        "--refresh-mojibake-baseline",
        action="store_true",
        default=False,
        help="Re-seed _mojibake_baseline.json.",
    )
    group.addoption(
        "--refresh-asymmetric-resource-guards-baseline",
        action="store_true",
        default=False,
        help="Re-seed _asymmetric_resource_guards_baseline.json.",
    )
    group.addoption(
        "--refresh-branch-coverage-baseline",
        action="store_true",
        default=False,
        help="Re-seed _branch_coverage_baseline.json (test_branch_coverage_ratchet.py).",
    )
    group.addoption(
        "--refresh-whitelist-drift-baseline",
        action="store_true",
        default=False,
        help="Re-seed _whitelist_drift_baseline.json (test_deferred_whitelist_drift.py).",
    )
    register_code_audit_refresh_option(parser)  # --refresh-code-audit-baseline, shared with every other consumer
    register_loc_budget_refresh_option(parser)  # --refresh-loc-budget-baseline
    register_readme_env_var_refresh_option(parser)  # --refresh-readme-env-var-baseline
