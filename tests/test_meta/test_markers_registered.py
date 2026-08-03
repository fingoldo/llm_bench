"""Regression sensor: every custom pytest marker used under tests/**.py
must be registered in pyproject.toml ``[tool.pytest.ini_options].markers``
(this repo has no conftest.py ``pytest_configure``/``addinivalue_line``
marker registration -- everything goes through pyproject.toml).

Under ``--strict-markers`` (active per this repo's ``addopts``), an
unregistered marker reference is a collection-time error. This sensor
AST-scans every ``pytest.mark.<name>`` attribute reference anywhere in a
module under ``tests/`` -- decorators, ``pytestmark`` assignments (plain
or annotated), aliased marks, and dynamic ``item.add_marker(...)`` calls
-- and asserts the marker name is registered. Cheap mechanical
set-difference; a hard assert-empty is used, no baseline ratchet needed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = _REPO_ROOT / "tests"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# Marks pytest registers itself; never need project-side registration.
_BUILTIN_MARKERS = frozenset({"skip", "skipif", "xfail", "parametrize", "usefixtures"})

# Marks registered by an installed plugin's own ``pytest_configure`` (via
# ``config.addinivalue_line("markers", ...)``), independent of this repo's
# pyproject.toml. Confirmed for "asyncio": pytest_asyncio.plugin.pytest_configure
# calls addinivalue_line("markers", "asyncio: ...") itself, and a full
# --strict-markers collection of this repo passes with "asyncio" absent
# from pyproject.toml's markers list. Same is true for "timeout": pytest-timeout
# (a declared dev dependency) self-registers "timeout" in its own
# pytest_configure via addinivalue_line -- a real @pytest.mark.timeout(...)
# use collects cleanly under --strict-markers with no pyproject entry.
_PLUGIN_REGISTERED_MARKERS = frozenset({"asyncio", "timeout"})


def _parse_pyproject_markers() -> set[str]:
    """Parse ``markers = ["name: descr", ...]`` under
    ``[tool.pytest.ini_options]`` in pyproject.toml. Returns marker names.
    """
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    with open(_PYPROJECT, "rb") as f:
        data = tomllib.load(f)
    raw = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", []) or []
    out: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        name = entry.split(":", 1)[0].strip()
        if name:
            out.add(name)
    return out


def _mark_names_in_node(node: ast.AST) -> list[str]:
    """Collect every ``pytest.mark.<name>`` attribute reference reachable
    from ``node`` -- handles a bare attribute (``pytest.mark.slow`` used
    directly in a ``pytestmark`` assignment or a marks-list entry), an
    attribute called as a decorator/marks factory (``pytest.mark.skipif(...)``),
    and list/tuple literals of either (``pytestmark = [pytest.mark.a, pytest.mark.b]``).
    """
    names: list[str] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Attribute):
            continue
        base = sub.value
        if isinstance(base, ast.Attribute) and base.attr == "mark" and isinstance(base.value, ast.Name) and base.value.id == "pytest":
            names.append(sub.attr)
    return names


def _scan_used_markers() -> dict[str, list[str]]:
    """Walk every ``.py`` file under ``tests/`` (excluding ``__pycache__``
    and this file) and collect every ``pytest.mark.<name>`` attribute
    reference anywhere in the module -- not just decorators and
    ``pytestmark = ...`` assignments. A whole-tree walk (rather than only
    inspecting decorator lists and ``pytestmark`` assign targets) also
    catches an aliased mark (``_slow = pytest.mark.x; @_slow``), a typed
    ``pytestmark: list = [...]`` (``ast.AnnAssign``, not ``ast.Assign``),
    and a dynamic ``item.add_marker(pytest.mark.x)`` inside a
    ``pytest_collection_modifyitems`` hook -- the exact pattern this repo's
    own ``tests/conftest.py`` already uses (with the builtin ``skip`` mark).
    Returns {marker_name: [files seen in]}.
    """
    seen: dict[str, list[str]] = {}
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        names = set(_mark_names_in_node(tree))
        rel = str(path.relative_to(_REPO_ROOT))
        for n in names:
            seen.setdefault(n, []).append(rel)
    return seen


def test_every_used_marker_is_registered():
    registered = _parse_pyproject_markers() | _BUILTIN_MARKERS | _PLUGIN_REGISTERED_MARKERS
    used = _scan_used_markers()

    unregistered = {name: paths for name, paths in used.items() if name not in registered}
    if unregistered:
        detail = "\n  ".join(f"{name}: seen in {paths}" for name, paths in sorted(unregistered.items()))
        pytest.fail(
            "Unregistered pytest markers found (collection under --strict-markers "
            f"errors):\n  {detail}\n\nAdd each to pyproject.toml [tool.pytest.ini_options].markers."
        )


def test_pyproject_marker_parser_picks_up_known_entries():
    """Smoke: parser sees the known registered names. Guards against a
    pyproject schema rename silently breaking the marker test above.
    """
    registered = _parse_pyproject_markers()
    for required in ("slow", "integration", "live", "postgres"):
        assert required in registered, f"Marker {required!r} missing from pyproject.toml; either pyproject was edited or the parser is broken."


class TestScanUsedMarkersDetectsShapes:
    """Direct regression tests for the checker itself: fires on synthetic
    sources carrying each marker-reference shape, stays silent on clean
    ones. Uses tmp_path so ``_scan_used_markers`` reads real files -- it
    is not parameterized to accept a source string directly.
    """

    def _run(self, tmp_path, monkeypatch, source: str) -> dict[str, list[str]]:
        """Route a synthetic test file through the REAL ``_scan_used_markers``
        (not a re-implementation) so this class stays a true regression test
        of the production scanner, not a parallel copy that can drift.
        """
        (tmp_path / "test_synthetic.py").write_text(source, encoding="utf-8")
        monkeypatch.setattr("tests.test_meta.test_markers_registered._TESTS_DIR", tmp_path)
        monkeypatch.setattr("tests.test_meta.test_markers_registered._REPO_ROOT", tmp_path)
        return _scan_used_markers()

    def test_decorator_mark_detected(self, tmp_path, monkeypatch):
        seen = self._run(tmp_path, monkeypatch, "import pytest\n\n@pytest.mark.made_up_marker\ndef test_x():\n    pass\n")
        assert "made_up_marker" in seen

    def test_class_decorator_mark_detected(self, tmp_path, monkeypatch):
        seen = self._run(
            tmp_path,
            monkeypatch,
            "import pytest\n\n@pytest.mark.made_up_marker\nclass TestX:\n    def test_a(self):\n        pass\n",
        )
        assert "made_up_marker" in seen

    def test_pytestmark_single_assignment_detected(self, tmp_path, monkeypatch):
        seen = self._run(tmp_path, monkeypatch, "import pytest\n\npytestmark = pytest.mark.made_up_marker\n\ndef test_x():\n    pass\n")
        assert "made_up_marker" in seen

    def test_pytestmark_list_assignment_detected(self, tmp_path, monkeypatch):
        seen = self._run(
            tmp_path,
            monkeypatch,
            "import pytest\n\npytestmark = [pytest.mark.made_up_marker, pytest.mark.slow]\n\ndef test_x():\n    pass\n",
        )
        assert "made_up_marker" in seen
        assert "slow" in seen

    def test_called_mark_decorator_detected(self, tmp_path, monkeypatch):
        seen = self._run(
            tmp_path,
            monkeypatch,
            'import pytest\n\n@pytest.mark.skipif(True, reason="x")\ndef test_x():\n    pass\n',
        )
        assert "skipif" in seen

    def test_clean_file_with_no_marks_not_flagged(self, tmp_path, monkeypatch):
        seen = self._run(tmp_path, monkeypatch, "def test_x():\n    assert True\n")
        assert seen == {}

    def test_aliased_mark_assignment_detected(self, tmp_path, monkeypatch):
        """``_alias = pytest.mark.x`` then ``@_alias`` -- the mark reference
        sits inside a plain ``Assign`` to a non-``pytestmark`` name.
        """
        seen = self._run(
            tmp_path,
            monkeypatch,
            "import pytest\n\n_slow = pytest.mark.made_up_marker\n\n@_slow\ndef test_x():\n    pass\n",
        )
        assert "made_up_marker" in seen

    def test_annotated_pytestmark_assignment_detected(self, tmp_path, monkeypatch):
        """``pytestmark: list = [...]`` is an ``ast.AnnAssign``, not an
        ``ast.Assign`` -- must still be caught.
        """
        seen = self._run(
            tmp_path,
            monkeypatch,
            "import pytest\n\npytestmark: list = [pytest.mark.made_up_marker]\n\ndef test_x():\n    pass\n",
        )
        assert "made_up_marker" in seen

    def test_dynamic_add_marker_call_detected(self, tmp_path, monkeypatch):
        """``item.add_marker(pytest.mark.x)`` inside a collection hook --
        the exact pattern this repo's own ``tests/conftest.py`` uses (with
        the builtin ``skip`` mark) -- must still be caught.
        """
        seen = self._run(
            tmp_path,
            monkeypatch,
            "import pytest\n\n"
            "def pytest_collection_modifyitems(config, items):\n"
            "    for item in items:\n"
            "        item.add_marker(pytest.mark.made_up_marker)\n",
        )
        assert "made_up_marker" in seen

    def test_full_checker_flags_unregistered_and_ignores_registered(self, tmp_path, monkeypatch):
        """End-to-end: a synthetic tests dir carrying one registered and one
        unregistered mark yields exactly the unregistered one as a violation.
        """
        seen = self._run(
            tmp_path,
            monkeypatch,
            "import pytest\n\n@pytest.mark.slow\ndef test_a():\n    pass\n\n@pytest.mark.totally_unregistered_marker\ndef test_b():\n    pass\n",
        )
        registered = _parse_pyproject_markers() | _BUILTIN_MARKERS | _PLUGIN_REGISTERED_MARKERS
        unregistered = {name for name in seen if name not in registered}
        assert unregistered == {"totally_unregistered_marker"}
