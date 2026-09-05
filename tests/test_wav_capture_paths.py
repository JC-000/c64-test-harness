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

import re
from pathlib import Path

import pytest

from wav_capture_paths import REFRESH_ENV, TRACKED_ROOT, capture_dir, refresh_requested

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

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


def test_default_destination_is_outside_the_repo(tmp_path: Path, monkeypatch) -> None:
    """(a) env var unset: captures land under the scratch dir, never in-tree."""
    monkeypatch.delenv(REFRESH_ENV, raising=False)
    dest = capture_dir("chromatic", tmp_path)
    assert _is_inside(dest, tmp_path), dest
    assert not _is_inside(dest, REPO_ROOT), dest
    assert dest.name == "chromatic"


def test_refresh_env_selects_the_tracked_fixture_dir(tmp_path: Path, monkeypatch) -> None:
    """(b) WAV_CAPTURES_REFRESH=1: captures refresh the committed reference."""
    monkeypatch.setenv(REFRESH_ENV, "1")
    dest = capture_dir("multi_sid", tmp_path)
    assert dest == TRACKED_ROOT / "multi_sid"
    assert _is_inside(dest, REPO_ROOT)
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


@pytest.mark.parametrize("module", LIVE_MODULES, ids=lambda p: p.name)
def test_live_capture_modules_use_the_helper(module: Path) -> None:
    """(c) structural: both live modules route through ``capture_dir`` and
    no longer build the tracked path themselves."""
    src = module.read_text()
    assert re.search(r"^from wav_capture_paths import .*\bcapture_dir\b", src, re.M), (
        f"{module.name} does not import capture_dir from wav_capture_paths"
    )
    assert "capture_dir(" in src, f"{module.name} never calls capture_dir()"
    literal = re.search(r"Path\(__file__\)[^\n]*[\"']wav_captures[\"']", src)
    assert literal is None, (
        f"{module.name} still builds the tracked wav_captures path itself: "
        f"{literal.group(0)!r}"
    )
