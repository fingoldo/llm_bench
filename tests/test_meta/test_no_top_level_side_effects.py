"""``import llm_bench`` must NOT touch the network, the disk, or open
DB connections at module-load time.

We assert this by importing the package in a freshly-spawned subprocess
(so other tests' fixtures can't pre-warm anything) under
``faulthandler``. If the import does any I/O the subprocess fails the
assertion or hangs (a 10 s timeout catches the hang).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

_PROBE = textwrap.dedent(
    """
    import socket, builtins, sys

    # Hard-fail any network attempt during import.
    _orig_create = socket.socket.__init__
    def _no_net(self, *a, **kw):
        raise AssertionError(
            f"socket() opened during import (args={a}, kwargs={kw})"
        )
    socket.socket.__init__ = _no_net

    # Hard-fail any file open of suspicious paths during import.
    # We allow site-packages / stdlib reads (Python imports use them) but
    # reject DB-style URLs and obvious data paths.
    _orig_open = builtins.open
    def _safe_open(file, *a, **kw):
        sf = str(file)
        forbidden = (".db", ".sqlite", "postgresql://", "postgres://")
        for token in forbidden:
            if token in sf:
                raise AssertionError(
                    f"open() of forbidden resource during import: {sf!r}"
                )
        return _orig_open(file, *a, **kw)
    builtins.open = _safe_open

    import llm_bench  # noqa: F401
    print("OK")
    """
).strip()


def test_top_level_import_pure():
    """Spawn a subprocess; ``import llm_bench`` must succeed without I/O."""
    res = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, timeout=15,
    )
    if res.returncode != 0 or "OK" not in res.stdout:
        pytest.fail(
            f"import llm_bench had top-level side effects.\n"
            f"  stdout: {res.stdout!r}\n"
            f"  stderr: {res.stderr!r}\n"
            f"Move the I/O behind an explicit factory call (e.g. "
            f"BenchmarkStorage.initialize(), Benchmark.run_phase())."
        )
