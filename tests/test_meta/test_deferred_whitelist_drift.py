"""Drift tracker: fail if any whitelist/grandfathered-exception constant
across tests/test_meta/ GROWS versus a committed baseline.

Nearly every meta-test in this suite carries its own escape hatch for
confirmed false positives -- a module-level ``_WHITELIST``, ``_EXCLUDE_DIRS``,
``_GRANDFATHERED_*``, ``_ALLOWLIST``/``_ALLOWED_*`` set, frozenset, list, or
dict literal (see test_no_unicode_in_console_output.py's ``_WHITELIST: set[str]
= set()`` for the template). Each individual entry is a reasonable, reviewed
judgment call at the time it's added -- but nothing stops the suite-wide total
from creeping up silently over time as more scanners get more exceptions,
which is exactly the debt-accumulation asymmetry this test exists to catch
(same rationale as pyutilz's tests/test_meta/test_deferred_drift.py, which
this file mirrors for the differently-named convention used in this repo).

This test:

  1. AST-scans every ``tests/test_meta/*.py`` file for module-level
     assignments (``Assign``/``AnnAssign``) whose target name matches the
     whitelist-shaped naming convention (``_WHITELIST``, ``_EXCLUDE*``,
     ``_GRANDFATHERED*``, ``_ALLOWLIST``/``*ALLOWLIST*``, ``_ALLOWED_*``,
     matched case-insensitively) and whose right-hand side is a literal
     ``set``/``frozenset``/``list``/``dict`` (either a literal display,
     e.g. ``{...}``/``[...]``, or a call to ``set()``/``frozenset()``/
     ``list()``/``dict()`` wrapping one) -- counting the literal entries
     via AST so the count survives reformatting and doesn't require
     importing the module. Tuple/list-unpacking targets (e.g.
     ``_WHITELIST, _OTHER = {1}, {2, 3}``) are flattened and matched
     element-for-element against the RHS.
  2. Also AST-scans every file for in-place mutation calls
     (``.add``/``.update``/``.append``/``.extend``) made anywhere on a
     whitelist-shaped name (e.g. ``_WHITELIST.add("x")``) -- these grow
     the real container invisibly to the literal-entry count above, so
     any such call is a hard failure rather than being silently
     uncounted.
  3. Compares the resulting ``{"<file>::<name>": count}`` map to a baseline
     stored in ``tests/test_meta/_whitelist_drift_baseline.json``.
  4. Fails if any collection's entry count GREW since the baseline. A
     shrink or steady count is fine and is reported informationally.
  5. This file's own ``_WHITELIST``-shaped constants are scanned like any
     other -- there's no self-exemption.

Refresh after intentionally accepting more whitelist debt::

    pytest tests/test_meta/test_deferred_whitelist_drift.py --refresh-whitelist-drift-baseline

Then commit the refreshed baseline. Reviewers see the diff and can push
back if the growth is unexplained.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

_TEST_META_DIR = Path(__file__).resolve().parent
_BASELINE_PATH = _TEST_META_DIR / "_whitelist_drift_baseline.json"
_REFRESH_FLAG = "--refresh-whitelist-drift-baseline"

# Module-level names matching this pattern are treated as whitelist/
# grandfathered-exception style escape hatches for this drift check.
_WHITELIST_NAME_RE = re.compile(r"(WHITELIST|EXCLUDE|GRANDFATHERED|ALLOWLIST|ALLOWED)", re.IGNORECASE)

# Container-constructor call names whose first positional arg (or, for a
# no-arg call, an implicit empty container) we unwrap to count entries.
_CONTAINER_CALL_NAMES = frozenset({"set", "frozenset", "list", "dict", "tuple"})

# In-place mutation methods that can grow a whitelist-shaped container
# *after* its declaration, invisible to the literal-entry count at
# assignment time (e.g. ``_WHITELIST.add("x")`` or ``_WHITELIST.update({...})``).
# Any such call on a whitelist-shaped name anywhere in the file is a hard
# failure -- it's a materially easier bypass than editing a literal, so it
# is flagged rather than silently uncounted.
_MUTATING_METHOD_NAMES = frozenset({"add", "update", "append", "extend"})

# Files/names with real false-positive risk for this scanner (e.g. a
# whitelist-shaped name that isn't actually a literal container, or a
# literal built from something other than a plain display/call). Empty
# for now -- add "<file.py>::<name>" entries with a one-line reason if a
# future scanner shape needs one.
_WHITELIST: set[str] = set()


def _literal_entry_count(node: ast.expr) -> "int | None":
    """Return the number of entries in a literal set/frozenset/list/dict/
    tuple display, or of one wrapped in a ``set()``/``frozenset()``/
    ``list()``/``dict()``/``tuple()`` call. Returns None if ``node`` isn't
    one of these shapes (e.g. a name reference or a comprehension) -- such
    assignments are simply not counted rather than mis-counted."""
    if isinstance(node, ast.Dict):
        return len(node.keys)
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return len(node.elts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CONTAINER_CALL_NAMES:
        if not node.args:
            return 0
        return _literal_entry_count(node.args[0])
    return None


def _flatten_targets(target: ast.expr) -> list[ast.expr]:
    """Flatten a (possibly nested) tuple/list assignment target into its
    leaf elements, e.g. ``_WHITELIST, _OTHER = ...`` or
    ``(_A, (_B, _C)) = ...``."""
    if isinstance(target, (ast.Tuple, ast.List)):
        leaves: list[ast.expr] = []
        for elt in target.elts:
            leaves.extend(_flatten_targets(elt))
        return leaves
    return [target]


def _scan_file(path: Path) -> dict[str, int]:
    """Return {"<name>": count} for every whitelist-shaped module-level
    assignment found in ``path``."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    found: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        # A tuple/list-unpacking assignment (e.g. ``_A, _B = {1}, {2, 3}``)
        # pairs each leaf target with the corresponding element of a
        # matching tuple/list RHS; anything else (e.g. unpacking a single
        # name reference) can't be resolved per-target and is left uncounted.
        for target in targets:
            leaves = _flatten_targets(target)
            if len(leaves) == 1:
                pairs = [(leaves[0], value)]
            elif isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(leaves):
                pairs = list(zip(leaves, value.elts, strict=True))
            else:
                continue
            for leaf, leaf_value in pairs:
                if not isinstance(leaf, ast.Name):
                    continue
                if not _WHITELIST_NAME_RE.search(leaf.id):
                    continue
                count = _literal_entry_count(leaf_value)
                if count is not None:
                    found[leaf.id] = count
    return found


def _scan_file_mutations(path: Path) -> list[str]:
    """Return a list of "<name>.<method>(...)" descriptions for every
    in-place mutation call (``.add``/``.update``/``.append``/``.extend``)
    made anywhere in ``path`` on a whitelist-shaped name -- these grow the
    real container invisibly to the literal-entry count taken at
    declaration time, so they're reported as a hard failure rather than
    silently left uncounted."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    mutations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _MUTATING_METHOD_NAMES:
            continue
        receiver = func.value
        if not isinstance(receiver, ast.Name) or not _WHITELIST_NAME_RE.search(receiver.id):
            continue
        mutations.append(f"{receiver.id}.{func.attr}(...) at line {node.lineno}")
    return mutations


def count_whitelist_entries(test_meta_dir: Path) -> dict[str, int]:
    """Scan every ``*.py`` file directly under ``test_meta_dir`` and return
    a flat ``{"<file.py>::<name>": count}`` map, honoring ``_WHITELIST``."""
    current: dict[str, int] = {}
    for path in sorted(test_meta_dir.glob("*.py")):
        rel = path.name
        for name, count in _scan_file(path).items():
            key = f"{rel}::{name}"
            if key in _WHITELIST:
                continue
            current[key] = count
    return current


def _refresh_requested() -> bool:
    return _REFRESH_FLAG in sys.argv


def _scan_mutations(test_meta_dir: Path) -> dict[str, list[str]]:
    """Scan every ``*.py`` file directly under ``test_meta_dir`` and return
    a flat ``{"<file.py>": [mutation, ...]}`` map of any in-place growth
    calls found on whitelist-shaped names."""
    out: dict[str, list[str]] = {}
    for path in sorted(test_meta_dir.glob("*.py")):
        found = _scan_file_mutations(path)
        if found:
            out[path.name] = found
    return out


def test_whitelist_collections_havent_grown():
    mutations = _scan_mutations(_TEST_META_DIR)
    if mutations:
        msg_parts = [
            "In-place mutation call(s) found on whitelist-shaped name(s) -- these "
            "grow the real whitelist invisibly to this drift check's literal-entry "
            "count and are disallowed outright (add entries via the literal "
            "display/constructor call instead):"
        ]
        for fname, calls in mutations.items():
            msg_parts.append(f"  {fname}:\n    " + "\n    ".join(calls))
        pytest.fail("\n".join(msg_parts))

    current = count_whitelist_entries(_TEST_META_DIR)

    if _refresh_requested() or not _BASELINE_PATH.exists():
        _BASELINE_PATH.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        total = sum(current.values())
        pytest.skip(f"whitelist-drift baseline refreshed at {_BASELINE_PATH.name} ({len(current)} collection(s), {total} total entry(ies))")

    baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    grown: list[str] = []
    new_keys: list[str] = []

    for key, count in current.items():
        old = baseline.get(key)
        if old is None:
            new_keys.append(f"{key}: NEW (now {count})")
        elif count > old:
            grown.append(f"{key}: {old} -> {count} (+{count - old})")

    if grown or new_keys:
        msg_parts = ["Whitelist/grandfathered-exception collection(s) GREW since baseline:"]
        if grown:
            msg_parts.append("  GROWN:\n    " + "\n    ".join(grown))
        if new_keys:
            msg_parts.append("  NEW:\n    " + "\n    ".join(new_keys))
        msg_parts.append(
            "  Either drain entries from the listed whitelists, OR refresh the "
            "baseline if the growth is a deliberate, reviewed decision:\n    "
            f"pytest tests/test_meta/test_deferred_whitelist_drift.py {_REFRESH_FLAG}"
        )
        pytest.fail("\n".join(msg_parts))

    shrunk = [f"{k}: {baseline[k]} -> {current[k]} (-{baseline[k] - current[k]})" for k in current if k in baseline and current[k] < baseline[k]]
    if shrunk:
        sys.stderr.write(f"\n[test_whitelist_collections_havent_grown] {len(shrunk)} whitelist(s) SHRANK - drained:\n  " + "\n  ".join(shrunk) + "\n")


class TestScanFileDetectsShapes:
    """Regression tests for the AST scanner itself, via synthetic source
    written to a real temp file (mirrors test_no_unicode_in_console_output.py's
    TestAuditFileDetectsAllThreeShapes convention)."""

    def _run(self, tmp_path, source: str) -> dict[str, int]:
        p = tmp_path / "test_synthetic.py"
        p.write_text(source, encoding="utf-8")
        return _scan_file(p)

    def test_fires_on_set_literal_annassign(self, tmp_path):
        found = self._run(tmp_path, "_WHITELIST: set[str] = {'a', 'b', 'c'}\n")
        assert found == {"_WHITELIST": 3}

    def test_fires_on_empty_set_call(self, tmp_path):
        found = self._run(tmp_path, "_WHITELIST: set[str] = set()\n")
        assert found == {"_WHITELIST": 0}

    def test_fires_on_frozenset_call_wrapping_literal(self, tmp_path):
        found = self._run(tmp_path, "_EXCLUDE_DIRS = frozenset({'a', 'b'})\n")
        assert found == {"_EXCLUDE_DIRS": 2}

    def test_fires_on_dict_literal(self, tmp_path):
        found = self._run(tmp_path, "_LOCAL_ONLY_ALLOWLIST: dict[str, str] = {'x': 'y', 'z': 'w'}\n")
        assert found == {"_LOCAL_ONLY_ALLOWLIST": 2}

    def test_fires_on_grandfathered_name(self, tmp_path):
        found = self._run(tmp_path, "_GRANDFATHERED = ['a', 'b', 'c', 'd']\n")
        assert found == {"_GRANDFATHERED": 4}

    def test_silent_on_non_whitelist_name(self, tmp_path):
        found = self._run(tmp_path, "_CANDIDATE_MARKERS = ('a', 'b', 'c')\n")
        assert found == {}

    def test_silent_on_non_literal_rhs(self, tmp_path):
        # A name reference (or any other non-literal shape) isn't a
        # statically-countable literal -- the scanner must not guess.
        found = self._run(tmp_path, "_OTHER = {'a'}\n_WHITELIST = _OTHER\n")
        assert found == {}

    def test_silent_on_non_module_level_assignment(self, tmp_path):
        found = self._run(tmp_path, "def f():\n    _WHITELIST = {'a', 'b'}\n    return _WHITELIST\n")
        assert found == {}

    def test_fires_on_tuple_unpacking_target(self, tmp_path):
        found = self._run(tmp_path, "_WHITELIST, _OTHER_EXCLUDE = {1, 2}, {3, 4, 5}\n")
        assert found == {"_WHITELIST": 2, "_OTHER_EXCLUDE": 3}

    def test_fires_case_insensitively_on_name(self, tmp_path):
        found = self._run(tmp_path, "_whitelist = {'a', 'b'}\n")
        assert found == {"_whitelist": 2}


class TestScanFileMutationsDetectsGrowth:
    """Regression tests for the in-place-mutation scanner: the bypass where
    a whitelist is declared small/empty and then grown via ``.add``/
    ``.update``/``.append``/``.extend`` after the fact."""

    def _run(self, tmp_path, source: str) -> list[str]:
        p = tmp_path / "test_synthetic.py"
        p.write_text(source, encoding="utf-8")
        return _scan_file_mutations(p)

    def test_fires_on_add_after_empty_set(self, tmp_path):
        found = self._run(tmp_path, "_WHITELIST: set[str] = set()\n_WHITELIST.add('new_fp')\n")
        assert len(found) == 1 and "add" in found[0]

    def test_fires_on_update_call(self, tmp_path):
        found = self._run(tmp_path, "_EXCLUDE_DIRS = {'a'}\n_EXCLUDE_DIRS.update({'b', 'c'})\n")
        assert len(found) == 1 and "update" in found[0]

    def test_fires_on_append_and_extend(self, tmp_path):
        found = self._run(
            tmp_path,
            "_ALLOWLIST = ['a']\n_ALLOWLIST.append('b')\n_ALLOWLIST.extend(['c', 'd'])\n",
        )
        assert len(found) == 2

    def test_silent_on_mutation_of_non_whitelist_name(self, tmp_path):
        found = self._run(tmp_path, "_CANDIDATES = set()\n_CANDIDATES.add('x')\n")
        assert found == []

    def test_silent_on_non_mutating_method_call(self, tmp_path):
        found = self._run(tmp_path, "_WHITELIST = {'a'}\nprint(_WHITELIST.copy())\n")
        assert found == []
