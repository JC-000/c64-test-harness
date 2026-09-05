"""Where the U64_HOST-gated capture tests write their output (issue #220).

``tests/test_chromatic_capture_live.py`` and
``tests/test_multi_sid_parallel_live.py`` used to write straight into the
tracked ``tests/wav_captures/`` tree, so every bench run dirtied the
working tree and silently re-based the committed reference.  The path
decision now lives in ``tests/wav_capture_paths.py``: scratch by default,
the tracked tree only under ``WAV_CAPTURES_REFRESH=1``.  No hardware is
needed to pin that: the live modules' ``wav_dir`` fixtures are called
directly here with a fake ``tmp_path_factory``.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from wav_capture_paths import REFRESH_ENV, TRACKED_ROOT, capture_dir, refresh_requested

TESTS_DIR = Path(__file__).resolve().parent

#: (module name, suite folder under tests/wav_captures/) for each live capture module.
LIVE_MODULES = (
    ("test_chromatic_capture_live", "chromatic"),
    ("test_multi_sid_parallel_live", "multi_sid"),
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


def test_scratch_that_lands_in_the_tracked_tree_is_refused(tmp_path: Path, monkeypatch) -> None:
    """The helper, not pytest's basetemp, is what keeps captures out of the
    reference.  The refusal is on the *destination*: a scratch root under
    tests/wav_captures/ is refused, and so is ``tests/`` itself (whose
    ``wav_captures/<suite>`` IS the tracked directory) -- directly or
    through a symlink."""
    monkeypatch.delenv(REFRESH_ENV, raising=False)
    link_to_tests = tmp_path / "t"
    link_to_tests.symlink_to(TRACKED_ROOT.parent)
    link_to_tracked = tmp_path / "w"
    link_to_tracked.symlink_to(TRACKED_ROOT)
    for scratch in (
        TRACKED_ROOT,
        TRACKED_ROOT / "scratch",
        TRACKED_ROOT / "chromatic" / "..",
        TRACKED_ROOT.parent,
        TRACKED_ROOT.parent / "wav_captures" / "..",
        link_to_tests,
        link_to_tracked,
    ):
        with pytest.raises(ValueError, match="tracked"):
            capture_dir("chromatic", scratch)
    # The refusal is about the destination, so a refresh (which ignores scratch) still works.
    assert capture_dir("chromatic", TRACKED_ROOT.parent, environ={REFRESH_ENV: "1"}) == TRACKED_ROOT / "chromatic"


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


# ---------------------------------------------------------------------------
# (c) the live modules really go through the helper -- behavioural, not
# source inspection: call each module's wav_dir fixture with a fake
# tmp_path_factory and check what comes back.
# ---------------------------------------------------------------------------

class _FakeTmpPathFactory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.made: list[Path] = []

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        path = self.root / f"{basename}{len(self.made)}"
        path.mkdir(parents=True, exist_ok=True)
        self.made.append(path)
        return path


def _live_module(name: str, monkeypatch):
    """Import a live module with no device: its skip marker must not bite."""
    monkeypatch.delenv("U64_HOST", raising=False)
    monkeypatch.delenv(REFRESH_ENV, raising=False)
    return importlib.import_module(name)


@pytest.mark.parametrize("name,suite", LIVE_MODULES, ids=[m[0] for m in LIVE_MODULES])
def test_live_module_wav_dir_is_the_helpers_answer(
    name: str, suite: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    module = _live_module(name, monkeypatch)
    fixture = getattr(module, "wav_dir", None)
    assert fixture is not None, f"{name} has no wav_dir fixture"
    func = getattr(fixture, "__wrapped__", fixture)

    factory = _FakeTmpPathFactory(tmp_path)
    recorded: list[tuple[str, str]] = []
    result = func(factory, lambda key, value: recorded.append((key, value)))

    assert len(factory.made) == 1, "wav_dir must take exactly one scratch dir from tmp_path_factory"
    scratch = factory.made[0]
    assert result == capture_dir(suite, scratch), (
        f"{name}: wav_dir returned {result}, not capture_dir({suite!r}, {scratch})"
    )
    assert result.resolve() == capture_dir(suite, scratch).resolve()
    assert _is_inside(result, tmp_path)
    assert not _is_inside(result, TRACKED_ROOT), result
    assert result.is_dir(), "wav_dir must create the directory it hands out"
    # Item 7: the location is discoverable after the run.
    assert recorded == [(f"{suite}_capture_dir", str(result))]
    assert str(result) in capsys.readouterr().out


@pytest.mark.parametrize("name,suite", LIVE_MODULES, ids=[m[0] for m in LIVE_MODULES])
def test_live_module_wav_dir_honours_refresh(
    name: str, suite: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    """With the knob set, the fixture's answer is the tracked directory --
    proving the env var reaches the fixture through the helper, not a copy."""
    module = _live_module(name, monkeypatch)
    monkeypatch.setenv(REFRESH_ENV, "1")
    func = getattr(module.wav_dir, "__wrapped__", module.wav_dir)
    result = func(_FakeTmpPathFactory(tmp_path), lambda key, value: None)
    assert result == TRACKED_ROOT / suite


@pytest.mark.parametrize("name,suite", LIVE_MODULES, ids=[m[0] for m in LIVE_MODULES])
def test_live_module_never_names_the_tracked_root(name: str, suite: str) -> None:
    src = (TESTS_DIR / f"{name}.py").read_text()
    assert "TRACKED_ROOT" not in src, f"{name} names TRACKED_ROOT"
    assert os.sep + "wav_captures" not in src.replace("tests/wav_captures/", ""), (
        f"{name} spells out a wav_captures path"
    )
