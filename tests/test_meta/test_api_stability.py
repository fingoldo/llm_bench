"""Public-API stability snapshot for llm_bench.

Captures the package's top-level ``__all__`` plus, for each name, a
small fingerprint (kind + signature/fields/methods). Renames and
removals fail until the snapshot is explicitly refreshed.

Refresh after an intentional API change:

  pytest tests/test_meta/test_api_stability.py --refresh-api-snapshot

Then commit the snapshot diff. Reviewers see exactly what shifted.

Additions to ``__all__`` are silent (a new public symbol doesn't break
downstream code). Only removals and signature shifts trigger failure.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import llm_bench

_SNAPSHOT_PATH = Path(__file__).resolve().parent / "_api_snapshot.json"


def _refresh_requested() -> bool:
    return "--refresh-api-snapshot" in sys.argv


def _capture(name: str) -> dict[str, Any]:
    """Fingerprint a single public symbol."""
    obj = getattr(llm_bench, name)
    fp: dict[str, Any] = {"name": name}
    if inspect.isclass(obj):
        fp["kind"] = "class"
        fp["bases"] = sorted(b.__name__ for b in obj.__bases__ if b is not object)
        # Public methods (no underscore prefix).
        fp["methods"] = sorted(m for m, v in vars(obj).items() if not m.startswith("_") and callable(v))
        # Dataclass fields when available.
        if hasattr(obj, "__dataclass_fields__"):
            fp["dataclass_fields"] = sorted(obj.__dataclass_fields__.keys())
    elif inspect.isfunction(obj) or inspect.ismethod(obj):
        fp["kind"] = "function"
        try:
            fp["signature"] = str(inspect.signature(obj))
        except (TypeError, ValueError):
            fp["signature"] = "<unavailable>"
    elif callable(obj):
        # NewType / Protocol / weird callables — record kind only.
        fp["kind"] = type(obj).__name__
    elif isinstance(obj, str):
        fp["kind"] = "str"
        # Don't record value — strings like __version__ change every release.
    else:
        fp["kind"] = type(obj).__name__
    return fp


def _build_snapshot() -> dict:
    public = sorted(getattr(llm_bench, "__all__", []))
    return {
        "package_all": public,
        "symbols": {name: _capture(name) for name in public},
    }


def test_public_api_matches_snapshot():
    current = _build_snapshot()
    if _refresh_requested() or not _SNAPSHOT_PATH.exists():
        _SNAPSHOT_PATH.write_text(
            json.dumps(current, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        n = len(current["package_all"])
        pytest.skip(f"snapshot refreshed at {_SNAPSHOT_PATH.name} ({n} public symbols)")

    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))

    # Removals & renames fail.
    missing = sorted(set(snapshot["package_all"]) - set(current["package_all"]))
    if missing:
        pytest.fail(
            f"public symbols REMOVED from llm_bench.__all__: {missing}\n"
            f"Either restore them or refresh the snapshot:\n"
            f"  pytest tests/test_meta/test_api_stability.py "
            f"--refresh-api-snapshot"
        )

    # Per-symbol fingerprint shifts fail (kind change, method dropped,
    # dataclass field renamed, etc.).
    drift: list[str] = []
    for name in snapshot["package_all"]:
        if name not in current["package_all"]:
            continue  # already reported as removed above
        old = snapshot["symbols"].get(name, {})
        new = current["symbols"].get(name, {})
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                drift.append(f"{name}.{key}: {old.get(key)!r} -> {new.get(key)!r}")
    if drift:
        msg = "\n  ".join(drift)
        pytest.fail(
            f"public-API fingerprint drift detected:\n  {msg}\n"
            f"Refresh the snapshot if intentional:\n"
            f"  pytest tests/test_meta/test_api_stability.py "
            f"--refresh-api-snapshot"
        )
