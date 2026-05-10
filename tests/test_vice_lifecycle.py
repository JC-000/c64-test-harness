"""Tests for new ViceConfig fields wired into ViceProcess argv construction.

Pure unit tests — no real x64sc spawn; ``subprocess.Popen`` is patched.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess


def _start_and_capture_args(cfg: ViceConfig, mock_popen: MagicMock) -> list[str]:
    mock_popen.return_value = MagicMock()
    proc = ViceProcess(cfg)
    proc.start()
    args = mock_popen.call_args[0][0]
    proc._proc = None  # avoid stop() touching the mock
    return list(args)


# ---------- defaults ----------

def test_new_fields_default_to_none_or_false():
    cfg = ViceConfig()
    assert cfg.load_snapshot is None
    assert cfg.event_recording_start is False
    assert cfg.event_image is None
    assert cfg.event_snapshot_mode is None
    assert cfg.event_snapshot_dir is None
    assert cfg.seed is None
    assert cfg.sound_record_driver is None
    assert cfg.sound_record_file is None
    assert cfg.exit_screenshot is None


@patch("subprocess.Popen")
def test_defaults_emit_no_new_flags(mock_popen):
    cfg = ViceConfig()
    args = _start_and_capture_args(cfg, mock_popen)
    for flag in (
        "-loadsnapshot",
        "-eventstart",
        "-eventimage",
        "-eventsnapshot",
        "-eventsnapshotdir",
        "-seed",
        "-soundrecord",
        "-recordfile",
        "-exitscreenshot",
    ):
        assert flag not in args, f"unexpected default flag: {flag}"


# ---------- per-field positive cases ----------

@patch("subprocess.Popen")
def test_load_snapshot_emits_flag_and_path(mock_popen):
    cfg = ViceConfig(load_snapshot="/tmp/state.vsf")
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-loadsnapshot")
    assert args[i + 1] == "/tmp/state.vsf"


@patch("subprocess.Popen")
def test_event_recording_start_emits_flag(mock_popen):
    cfg = ViceConfig(event_recording_start=True)
    args = _start_and_capture_args(cfg, mock_popen)
    assert "-eventstart" in args


@patch("subprocess.Popen")
def test_event_image_emits_flag_and_path(mock_popen):
    cfg = ViceConfig(event_image="/tmp/events.bin")
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-eventimage")
    assert args[i + 1] == "/tmp/events.bin"


@pytest.mark.parametrize("mode", [0, 1, 2])
@patch("subprocess.Popen")
def test_event_snapshot_mode_valid_values(mock_popen, mode):
    cfg = ViceConfig(event_snapshot_mode=mode)
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-eventsnapshot")
    assert args[i + 1] == str(mode)


@patch("subprocess.Popen")
def test_event_snapshot_dir_emits_flag_and_path(mock_popen):
    cfg = ViceConfig(event_snapshot_dir="/var/tmp/snaps")
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-eventsnapshotdir")
    assert args[i + 1] == "/var/tmp/snaps"


@patch("subprocess.Popen")
def test_seed_emits_flag_and_int(mock_popen):
    cfg = ViceConfig(seed=42)
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-seed")
    assert args[i + 1] == "42"


@patch("subprocess.Popen")
def test_sound_record_driver_emits_flag(mock_popen):
    cfg = ViceConfig(sound_record_driver="wav")
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-soundrecord")
    assert args[i + 1] == "wav"


@patch("subprocess.Popen")
def test_sound_record_file_emits_flag(mock_popen):
    cfg = ViceConfig(sound_record_file="/tmp/out.wav")
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-recordfile")
    assert args[i + 1] == "/tmp/out.wav"


@patch("subprocess.Popen")
def test_exit_screenshot_emits_flag(mock_popen):
    cfg = ViceConfig(exit_screenshot="/tmp/exit.png")
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-exitscreenshot")
    assert args[i + 1] == "/tmp/exit.png"


# ---------- validation ----------

@pytest.mark.parametrize("bad", [-1, 3, 99])
def test_event_snapshot_mode_out_of_range_raises(bad):
    cfg = ViceConfig(event_snapshot_mode=bad)
    proc = ViceProcess(cfg)
    with pytest.raises(ValueError, match="event_snapshot_mode"):
        proc.start()


@patch("subprocess.Popen")
def test_paths_passed_unquoted_as_separate_tokens(mock_popen):
    """Paths with spaces are passed as a single token, no shell-quoting."""
    cfg = ViceConfig(load_snapshot="/tmp/has space/state.vsf")
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-loadsnapshot")
    assert args[i + 1] == "/tmp/has space/state.vsf"


@patch("c64_test_harness.backends.vice_lifecycle.sys.platform", "darwin")
@patch("subprocess.Popen")
def test_autostart_adds_prgmode_1_on_darwin(mock_popen):
    cfg = ViceConfig(prg_path="/tmp/foo.prg")
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-autostart")
    assert args[i + 1] == "/tmp/foo.prg"
    j = args.index("-autostartprgmode")
    assert args[j + 1] == "1"
    assert j > i


@patch("c64_test_harness.backends.vice_lifecycle.sys.platform", "linux")
@patch("subprocess.Popen")
def test_autostart_no_prgmode_on_linux(mock_popen):
    cfg = ViceConfig(prg_path="/tmp/foo.prg")
    args = _start_and_capture_args(cfg, mock_popen)
    assert "-autostart" in args
    assert "-autostartprgmode" not in args


@patch("c64_test_harness.backends.vice_lifecycle.sys.platform", "darwin")
@patch("subprocess.Popen")
def test_autostart_extra_args_override_wins_on_darwin(mock_popen):
    cfg = ViceConfig(prg_path="/tmp/foo.prg", extra_args=["-autostartprgmode", "0"])
    args = _start_and_capture_args(cfg, mock_popen)
    occurrences = [k for k, a in enumerate(args) if a == "-autostartprgmode"]
    assert len(occurrences) == 1
    assert args[occurrences[0] + 1] == "0"
