"""Tests for new ViceConfig fields wired into ViceProcess argv construction.

Pure unit tests — no real x64sc spawn; ``subprocess.Popen`` is patched.
"""
from __future__ import annotations

import subprocess
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
    assert cfg.event_image_include is None
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
        "-eventstartmode",
        "-eventsnapshotdir",
        "-eventimageinc",
        "+eventimageinc",
        "-seed",
        "-soundrecdev",
        "-soundrecarg",
        "-exitscreenshot",
    ):
        assert flag not in args, f"unexpected default flag: {flag}"


# ---------- per-field positive cases ----------

@pytest.mark.parametrize("mode", [0, 1, 2, 3])
@patch("subprocess.Popen")
def test_event_snapshot_mode_valid_values(mock_popen, mode):
    cfg = ViceConfig(event_snapshot_mode=mode)
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-eventstartmode")
    assert args[i + 1] == str(mode)


@pytest.mark.parametrize(
    "value, expected", [(True, "-eventimageinc"), (False, "+eventimageinc")]
)
@patch("subprocess.Popen")
def test_event_image_include_emits_the_right_polarity(mock_popen, value, expected):
    """VICE's cmdline convention: a leading ``+`` is the disabling form."""
    cfg = ViceConfig(event_image_include=value)
    args = _start_and_capture_args(cfg, mock_popen)
    assert expected in args
    other = "+eventimageinc" if value else "-eventimageinc"
    assert other not in args


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
    i = args.index("-soundrecdev")
    assert args[i + 1] == "wav"


@patch("subprocess.Popen")
def test_sound_record_file_emits_flag(mock_popen):
    cfg = ViceConfig(sound_record_file="/tmp/out.wav")
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-soundrecarg")
    assert args[i + 1] == "/tmp/out.wav"


@patch("subprocess.Popen")
def test_exit_screenshot_emits_flag(mock_popen):
    cfg = ViceConfig(exit_screenshot="/tmp/exit.png")
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-exitscreenshot")
    assert args[i + 1] == "/tmp/exit.png"


# ---------- validation ----------

@pytest.mark.parametrize("bad", [-1, 4, 99])
def test_event_snapshot_mode_out_of_range_raises(bad):
    cfg = ViceConfig(event_snapshot_mode=bad)
    proc = ViceProcess(cfg)
    with pytest.raises(ValueError, match="event_snapshot_mode"):
        proc.start()


@patch("subprocess.Popen")
def test_paths_passed_unquoted_as_separate_tokens(mock_popen):
    """Paths with spaces are passed as a single token, no shell-quoting."""
    cfg = ViceConfig(event_snapshot_dir="/tmp/has space/snaps")
    args = _start_and_capture_args(cfg, mock_popen)
    i = args.index("-eventsnapshotdir")
    assert args[i + 1] == "/tmp/has space/snaps"


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


# ---------- stop() must reap after kill() (no zombies) ----------

class TestStopReapsProcess:
    def test_stop_waits_after_kill_when_terminate_raises(self):
        """Non-sudo path: kill() must be followed by wait() so the dead
        process is reaped (BSD ps keeps comm names on zombies)."""
        proc = ViceProcess(ViceConfig())
        mock_proc = MagicMock()
        mock_proc.terminate.side_effect = OSError("cannot signal")
        proc._proc = mock_proc
        proc.stop()
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once_with(timeout=3)
        assert proc._proc is None

    def test_stop_waits_after_kill_on_terminate_timeout(self):
        """Non-sudo path: SIGTERM times out -> SIGKILL -> wait() reaps."""
        proc = ViceProcess(ViceConfig())
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired("x64sc", 5),
            0,
        ]
        proc._proc = mock_proc
        proc.stop()
        mock_proc.kill.assert_called_once()
        assert mock_proc.wait.call_count == 2
        # The wait after kill() must reap, i.e. come after kill().
        kill_index = [c[0] for c in mock_proc.method_calls].index("kill")
        last_wait_index = max(
            i for i, c in enumerate(mock_proc.method_calls) if c[0] == "wait"
        )
        assert last_wait_index > kill_index
        assert proc._proc is None

    @patch("subprocess.run")
    def test_sudo_stop_waits_after_last_resort_kill(self, mock_run):
        """Sudo path: after the last-resort kill() of the sudo wrapper,
        wait() must reap it before the handle is dropped."""
        proc = ViceProcess(ViceConfig())
        proc._is_sudo_child = True
        mock_proc = MagicMock()
        mock_proc.pid = 4321
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired("sudo", 5),
            subprocess.TimeoutExpired("sudo", 3),
            0,
        ]
        proc._proc = mock_proc
        # ps output with no matching x64sc child -> skip `sudo -n kill`
        mock_run.return_value = MagicMock(stdout="")
        proc.stop()
        mock_proc.kill.assert_called_once()
        assert mock_proc.wait.call_count == 3
        assert proc._proc is None


# ---------- sudo child PID resolution helpers ----------

class TestResolveVicePid:
    def test_non_sudo_resolves_to_popen_pid(self):
        proc = ViceProcess(ViceConfig())
        mock_proc = MagicMock()
        mock_proc.pid = 777
        proc._proc = mock_proc
        assert proc.is_sudo_child is False
        assert proc.resolve_vice_pid() == 777
        proc._proc = None

    @patch("subprocess.run")
    def test_sudo_resolves_x64sc_child(self, mock_run):
        proc = ViceProcess(ViceConfig())
        proc._is_sudo_child = True
        mock_proc = MagicMock()
        mock_proc.pid = 500
        proc._proc = mock_proc
        mock_run.return_value = MagicMock(
            stdout=(
                "  501   500 /usr/local/bin/x64sc\n"
                "  600     1 bash\n"
            )
        )
        assert proc.is_sudo_child is True
        assert proc.resolve_vice_pid() == 501
        proc._proc = None

    @patch("subprocess.run")
    def test_sudo_resolve_returns_none_when_no_child(self, mock_run):
        proc = ViceProcess(ViceConfig())
        proc._is_sudo_child = True
        mock_proc = MagicMock()
        mock_proc.pid = 500
        proc._proc = mock_proc
        mock_run.return_value = MagicMock(stdout="  600     1 bash\n")
        assert proc.resolve_vice_pid() is None
        proc._proc = None
