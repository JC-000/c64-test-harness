"""Where the U64_HOST-gated capture tests write their output (issue #220).

``tests/test_chromatic_capture_live.py`` and
``tests/test_multi_sid_parallel_live.py`` used to write straight into the
tracked ``tests/wav_captures/`` tree, so every bench run dirtied the
working tree and silently re-based the committed reference.  The path
decision now lives in ``tests/wav_capture_paths.py``: scratch by default,
the tracked tree only under ``WAV_CAPTURES_REFRESH=1``.  No hardware is
needed to pin that.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from wav_capture_paths import REFRESH_ENV, TRACKED_ROOT, capture_dir, refresh_requested

TESTS_DIR = Path(__file__).resolve().parent

LIVE_MODULES = (
    TESTS_DIR / "test_chromatic_capture_live.py",
    TESTS_DIR / "test_multi_sid_parallel_live.py",
)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def test_tracked_root_is_the_committed_fixture_tree() -> None:
    assert TRACKED_ROOT == TESTS_DIR / "wav_captures"
    assert (TRACKED_ROOT / "chromatic").is_dir()
    assert (TRACKED_ROOT / "multi_sid").is_dir()


def test_default_destination_is_outside_the_tracked_tree(tmp_path: Path, monkeypatch) -> None:
    """(a) env var unset: captures land under the scratch dir, never in the
    tracked fixture tree -- whatever pytest's basetemp is."""
    monkeypatch.delenv(REFRESH_ENV, raising=False)
    dest = capture_dir("chromatic", tmp_path)
    assert _is_inside(dest, tmp_path), dest
    assert not _is_inside(dest, TRACKED_ROOT), dest
    assert dest.name == "chromatic"


def test_scratch_inside_the_tracked_tree_is_refused(monkeypatch) -> None:
    """The helper, not pytest's basetemp, is what keeps captures out of the
    reference: a scratch root under tests/wav_captures/ is refused outright."""
    monkeypatch.delenv(REFRESH_ENV, raising=False)
    for scratch in (TRACKED_ROOT, TRACKED_ROOT / "scratch", TRACKED_ROOT / "chromatic" / ".."):
        with pytest.raises(ValueError, match="tracked"):
            capture_dir("chromatic", scratch)
    # The refusal is about the scratch root, so a refresh (which ignores it) still works.
    assert capture_dir("chromatic", TRACKED_ROOT / "scratch", environ={REFRESH_ENV: "1"}) == TRACKED_ROOT / "chromatic"


def test_refresh_env_selects_the_tracked_fixture_dir(tmp_path: Path, monkeypatch) -> None:
    """(b) WAV_CAPTURES_REFRESH=1: captures refresh the committed reference."""
    monkeypatch.setenv(REFRESH_ENV, "1")
    dest = capture_dir("multi_sid", tmp_path)
    assert dest == TRACKED_ROOT / "multi_sid"
    assert _is_inside(dest, TRACKED_ROOT)
    assert not _is_inside(dest, tmp_path)


@pytest.mark.parametrize("value", ["", "0", "no", "false", "off", " "])
def test_refresh_env_falsy_values_stay_in_scratch(tmp_path: Path, monkeypatch, value: str) -> None:
    monkeypatch.setenv(REFRESH_ENV, value)
    assert refresh_requested() is False
    assert _is_inside(capture_dir("chromatic", tmp_path), tmp_path)


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_refresh_env_truthy_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv(REFRESH_ENV, value)
    assert refresh_requested() is True


def test_explicit_environ_mapping_overrides_process_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(REFRESH_ENV, "1")
    assert _is_inside(capture_dir("chromatic", tmp_path, environ={}), tmp_path)
    monkeypatch.delenv(REFRESH_ENV)
    assert capture_dir("chromatic", tmp_path, environ={REFRESH_ENV: "1"}) == TRACKED_ROOT / "chromatic"


def test_suite_name_must_be_a_plain_component(tmp_path: Path) -> None:
    for bad in ("", "../x", "a/b", "/abs"):
        with pytest.raises(ValueError):
            capture_dir(bad, tmp_path, environ={})


def _joined_text(node: ast.AST) -> str:
    """Literal text of a str Constant or the constant parts of an f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return ""


@pytest.mark.parametrize("module", LIVE_MODULES, ids=lambda p: p.name)
def test_live_capture_modules_use_the_helper(module: Path) -> None:
    """(c) structural: both live modules route through ``capture_dir`` and
    nothing else -- no TRACKED_ROOT, no hand-built path -- and every WAV /
    JSON path they build hangs off the ``wav_dir`` fixture value."""
    src = module.read_text()
    tree = ast.parse(src, filename=str(module))

    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "wav_capture_paths"
        for alias in node.names
    }
    assert "capture_dir" in imported, f"{module.name} does not import capture_dir"
    assert "TRACKED_ROOT" not in src, f"{module.name} names TRACKED_ROOT"
    literal = re.search(r"Path\(__file__\)[^\n]*[\"']wav_captures[\"']", src)
    assert literal is None, f"{module.name} builds the tracked path itself: {literal.group(0)!r}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "wav_captures":
            raise AssertionError(f"{module.name} has a bare 'wav_captures' literal")

    # The wav_dir fixture returns exactly what capture_dir() handed it.
    fixtures = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "wav_dir"]
    assert len(fixtures) == 1, f"{module.name} has no wav_dir fixture"
    fixture = fixtures[0]
    returns = [n for n in ast.walk(fixture) if isinstance(n, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Name), (
        f"{module.name}: wav_dir must return a single name"
    )
    returned = returns[0].value.id
    sources = [
        n.value
        for n in fixture.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == returned for t in n.targets)
    ]
    assert len(sources) == 1, f"{module.name}: {returned!r} must be assigned exactly once in wav_dir"
    call = sources[0]
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "capture_dir", (
        f"{module.name}: wav_dir returns {returned!r}, which is not the result of capture_dir()"
    )

    # Every `<x> / "...wav"` or `<x> / "...json"` in the module is `wav_dir / ...`.
    joined = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        text = _joined_text(node.right)
        if not (text.endswith(".wav") or text.endswith(".json")):
            continue
        joined += 1
        assert isinstance(node.left, ast.Name) and node.left.id == "wav_dir", (
            f"{module.name}:{node.lineno}: capture path {text!r} is not built from wav_dir"
        )
    assert joined >= 1, f"{module.name} builds no capture path from wav_dir"
