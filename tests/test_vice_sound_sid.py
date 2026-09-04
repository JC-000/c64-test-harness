"""VICE sound configuration vs SID emulation (issues #193, #196.3).

Everything here is offline: ``subprocess.Popen`` is mocked, so no x64sc
is ever spawned.  The behavioural claims about VICE come from the 3.10
sources (``~/Documents/vice-src/3.10``) and are cited per test:

- ``src/sid/sid.c:130-142`` (``sid_read_off``) and ``sid.c:276-284``
  (the ``val < 0`` fallback) -- with the sound core off, ``$D41B`` and
  ``$D41C`` return ``maincpu_clk % 256``, ``$D419``/``$D41A`` return
  ``0xff``, and everything else returns ``0``.
- ``src/sound.c:1528`` -- under warp with no record device, the sample
  buffer is discarded.
- ``src/sound.c:1573-1613`` -- the loop that writes to the play *and*
  record devices is ``while (!warp_mode_enabled)``, so configuring
  ``-soundrecdev`` does not rescue a warped capture either.
- ``src/sound.c:1441-1449`` -- at volume 0, ``amp`` is 0 and the buffer
  is ``memset`` to zero *after* the SID has been clocked at
  ``sound.c:1432``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import vice_lifecycle
from c64_test_harness.backends.render_wav import _reject_silent_capture
from c64_test_harness.backends.vice_lifecycle import (
    NON_DRAINING_SOUND_DEVICES,
    SID_CLOCK_LEAK_REGISTERS,
    SID_REGISTER_FIRST,
    SID_REGISTER_LAST,
    ViceConfig,
    ViceProcess,
    headless_sid_config,
    sid_emulation_enabled,
    sid_sound_device_drains,
    warn_if_sid_reads_unemulated,
)

_LOGGER = vice_lifecycle.__name__


@pytest.fixture(autouse=True)
def _reset_once_per_process_warning():
    """The sound-disabled warning fires once per process by design.

    Without this reset only the first test in the file would ever see
    it, and the others would pass for the wrong reason.
    """
    vice_lifecycle._warned_sid_unemulated = False
    yield
    vice_lifecycle._warned_sid_unemulated = False


def _launch_args(cfg: ViceConfig) -> list[str]:
    """Argv ``ViceProcess`` would hand to ``Popen`` for *cfg*."""
    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        with ViceProcess(cfg):
            return list(mock_popen.call_args[0][0])


# --------------------------------------------------------------------------- #
# The predicate                                                               #
# --------------------------------------------------------------------------- #
class TestSidEmulationPredicate:
    def test_harness_default_does_not_emulate_the_sid(self) -> None:
        """The default config is the broken one -- that is the finding."""
        assert ViceConfig().sound is False
        assert sid_emulation_enabled(ViceConfig()) is False

    def test_sound_true_emulates(self) -> None:
        assert sid_emulation_enabled(ViceConfig(sound=True)) is True

    def test_a_sound_device_forces_emulation_even_with_sound_false(self) -> None:
        """``start()`` emits ``-sound`` whenever ``sounddev`` is set, so
        the predicate must agree with the argv it will build."""
        cfg = ViceConfig(sound=False, sounddev="wav", soundarg="/tmp/x.wav")
        assert sid_emulation_enabled(cfg) is True
        args = _launch_args(cfg)
        assert "-sound" in args
        assert "+sound" not in args

    def test_dummy_device_enables_the_core_but_does_not_drain(self) -> None:
        """The second broken mode: enabled, but the SID stops advancing."""
        cfg = ViceConfig(sound=True, sounddev="dummy")
        assert sid_emulation_enabled(cfg) is True
        assert sid_sound_device_drains(cfg) is False
        assert "dummy" in NON_DRAINING_SOUND_DEVICES

    def test_wav_device_drains(self) -> None:
        assert sid_sound_device_drains(ViceConfig(sounddev="wav")) is True

    def test_unset_device_counts_as_draining(self) -> None:
        """VICE picks a platform default; unknowable from here."""
        assert sid_sound_device_drains(ViceConfig(sound=True)) is True


# --------------------------------------------------------------------------- #
# The read-path warning                                                       #
# --------------------------------------------------------------------------- #
class TestWarnIfSidReadsUnemulated:
    def test_warns_on_osc3_read_with_sound_off(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            warned = warn_if_sid_reads_unemulated(ViceConfig(), 0xD41B, 1)
        assert warned is True
        assert "maincpu_clk" in caplog.text

    def test_names_the_leaking_registers_in_a_spanning_read(
        self, caplog
    ) -> None:
        """A whole-window read must still say which bytes are the clock."""
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            warn_if_sid_reads_unemulated(ViceConfig(), SID_REGISTER_FIRST, 32)
        for reg in SID_CLOCK_LEAK_REGISTERS:
            assert f"${reg:04X}" in caplog.text

    def test_silent_when_the_sid_is_emulated(self, caplog) -> None:
        cfg = ViceConfig(sound=True, sounddev="wav", soundarg="/tmp/x.wav")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            warned = warn_if_sid_reads_unemulated(cfg, 0xD41B, 1)
        assert warned is False
        assert caplog.text == ""

    @pytest.mark.parametrize(
        "addr,length",
        [
            (SID_REGISTER_FIRST - 1, 1),   # just below
            (SID_REGISTER_LAST + 1, 1),    # just above
            (0x0400, 1000),                # screen RAM, nowhere near
            (0xD41B, 0),                   # zero-length read
        ],
    )
    def test_silent_outside_the_sid_window(self, caplog, addr, length) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert warn_if_sid_reads_unemulated(ViceConfig(), addr, length) is False

    def test_a_read_straddling_the_window_start_still_warns(self, caplog) -> None:
        """$D3FF..$D400 touches the window by one byte."""
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert warn_if_sid_reads_unemulated(
                ViceConfig(), SID_REGISTER_FIRST - 1, 2
            ) is True


# --------------------------------------------------------------------------- #
# Launch-time warnings                                                        #
# --------------------------------------------------------------------------- #
class TestLaunchWarnings:
    def test_default_launch_warns_that_the_sid_is_not_emulated(
        self, caplog
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _launch_args(ViceConfig())
        assert "reSID is not clocked" in caplog.text

    def test_the_warning_is_once_per_process(self, caplog) -> None:
        """Deduped deliberately: it is a static property of the config,
        and a per-launch line would drown a parallel run."""
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _launch_args(ViceConfig())
            _launch_args(ViceConfig())
        assert caplog.text.count("reSID is not clocked") == 1

    def test_dummy_device_launch_warns_about_the_stall(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _launch_args(ViceConfig(sound=True, sounddev="dummy"))
        assert "never drains" in caplog.text
        assert "reSID is not clocked" not in caplog.text

    def test_warp_plus_sound_device_warns_the_capture_will_be_empty(
        self, caplog
    ) -> None:
        cfg = ViceConfig(
            warp=True, sound=True, sounddev="wav", soundarg="/tmp/x.wav"
        )
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _launch_args(cfg)
        assert "discards the sample buffer under warp" in caplog.text

    def test_no_warp_warning_when_the_audio_is_deliberately_silence(
        self, caplog
    ) -> None:
        """``soundvolume=0`` says the device is there to drain, not to
        record, so warp discarding the samples is not news."""
        cfg = ViceConfig(
            warp=True,
            sound=True,
            sounddev="wav",
            soundarg="/tmp/x.wav",
            soundvolume=0,
        )
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _launch_args(cfg)
        assert "discards the sample buffer under warp" not in caplog.text

    def test_no_warp_warning_when_warp_is_off(self, caplog) -> None:
        cfg = ViceConfig(
            warp=False, sound=True, sounddev="wav", soundarg="/tmp/x.wav"
        )
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _launch_args(cfg)
        assert "discards the sample buffer under warp" not in caplog.text


# --------------------------------------------------------------------------- #
# -soundvolume                                                                #
# --------------------------------------------------------------------------- #
class TestSoundVolume:
    def test_absent_by_default(self) -> None:
        assert ViceConfig().soundvolume is None
        assert "-soundvolume" not in _launch_args(ViceConfig())

    def test_zero_is_emitted(self) -> None:
        """0 is a real value, not a falsy "unset" -- the bug this guards."""
        args = _launch_args(ViceConfig(soundvolume=0))
        assert args[args.index("-soundvolume") + 1] == "0"

    def test_value_is_emitted(self) -> None:
        args = _launch_args(ViceConfig(soundvolume=75))
        assert args[args.index("-soundvolume") + 1] == "75"

    @pytest.mark.parametrize("bad", [-1, 101, 1000])
    def test_out_of_range_refused(self, bad) -> None:
        with pytest.raises(ValueError, match="0..100"):
            _launch_args(ViceConfig(soundvolume=bad))

    def test_bool_refused(self) -> None:
        """``True`` would silently become volume 1."""
        with pytest.raises(ValueError, match="int in 0..100"):
            _launch_args(ViceConfig(soundvolume=True))


# --------------------------------------------------------------------------- #
# The healthy headless recipe                                                 #
# --------------------------------------------------------------------------- #
class TestHeadlessSidConfig:
    def test_produces_the_only_healthy_headless_combination(
        self, tmp_path
    ) -> None:
        wav = tmp_path / "drain.wav"
        cfg = headless_sid_config(wav)
        assert cfg.sound is True
        assert cfg.sounddev == "wav"
        assert cfg.soundarg == str(wav)
        assert sid_emulation_enabled(cfg)
        assert sid_sound_device_drains(cfg)

    def test_silent_by_default_and_optional(self, tmp_path) -> None:
        assert headless_sid_config(tmp_path / "a.wav").soundvolume == 0
        assert headless_sid_config(tmp_path / "b.wav", silent=False).soundvolume is None

    def test_argv_carries_sound_on_and_volume_zero(self, tmp_path) -> None:
        args = _launch_args(headless_sid_config(tmp_path / "a.wav"))
        assert "-sound" in args and "+sound" not in args
        assert args[args.index("-sounddev") + 1] == "wav"
        assert args[args.index("-soundvolume") + 1] == "0"

    def test_launching_it_emits_no_sound_warning_at_all(self, caplog, tmp_path) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _launch_args(headless_sid_config(tmp_path / "a.wav"))
        assert caplog.text == ""

    def test_inherits_from_a_base_config(self, tmp_path) -> None:
        base = ViceConfig(
            executable="/opt/x64sc",
            prg_path="/tmp/t.prg",
            port=6543,
            ntsc=False,
            extra_args=["-mycustom"],
        )
        cfg = headless_sid_config(tmp_path / "a.wav", base=base)
        assert cfg.executable == "/opt/x64sc"
        assert cfg.prg_path == "/tmp/t.prg"
        assert cfg.port == 6543
        assert cfg.ntsc is False
        assert cfg.extra_args == ["-mycustom"]

    def test_allocates_a_temp_wav_when_none_given(self) -> None:
        cfg = headless_sid_config()
        path = Path(cfg.soundarg)
        try:
            assert path.exists()
            assert path.suffix == ".wav"
        finally:
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# render_wav's silent-capture guard (#196.3)                                  #
# --------------------------------------------------------------------------- #
class TestRejectSilentCapture:
    def test_none_config_is_fine(self) -> None:
        _reject_silent_capture(None)

    def test_plain_config_is_fine(self) -> None:
        _reject_silent_capture(ViceConfig())

    @pytest.mark.parametrize("arg", ["-warp", "-warp=1", "+warp=0"])
    def test_warp_smuggled_through_extra_args_is_refused(self, arg) -> None:
        with pytest.raises(ValueError, match="cannot record under warp"):
            _reject_silent_capture(ViceConfig(extra_args=[arg]))

    def test_volume_zero_is_refused(self) -> None:
        with pytest.raises(ValueError, match="soundvolume=0"):
            _reject_silent_capture(ViceConfig(soundvolume=0))

    def test_a_nonzero_volume_is_allowed(self) -> None:
        _reject_silent_capture(ViceConfig(soundvolume=100))

    def test_render_wav_itself_refuses_a_smuggled_warp(self, tmp_path) -> None:
        """Through the public entry point, not just the private guard."""
        from c64_test_harness.backends.render_wav import render_wav

        prg = tmp_path / "t.prg"
        prg.write_bytes(b"\x01\x08\x60")
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            with pytest.raises(ValueError, match="cannot record under warp"):
                render_wav(
                    prg,
                    tmp_path / "out.wav",
                    duration_seconds=0.01,
                    config=ViceConfig(extra_args=["-warp"]),
                )
            mock_popen.assert_not_called()

    def test_render_wav_itself_refuses_volume_zero(self, tmp_path) -> None:
        from c64_test_harness.backends.render_wav import render_wav

        prg = tmp_path / "t.prg"
        prg.write_bytes(b"\x01\x08\x60")
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            with pytest.raises(ValueError, match="soundvolume=0"):
                render_wav(
                    prg,
                    tmp_path / "out.wav",
                    duration_seconds=0.01,
                    config=ViceConfig(soundvolume=0),
                )
            mock_popen.assert_not_called()

    def test_render_wav_still_forces_warp_off(self, tmp_path) -> None:
        """The positive half: with no smuggled flag, argv says +warp."""
        prg = tmp_path / "t.prg"
        prg.write_bytes(b"\x01\x08\x60")
        wav = tmp_path / "out.wav"
        from c64_test_harness.backends.render_wav import render_wav

        with patch("subprocess.Popen") as mock_popen:
            proc = MagicMock()
            proc.wait.return_value = 1
            proc.poll.return_value = 1
            mock_popen.return_value = proc
            wav.write_bytes(b"RIFF....")
            with patch.object(
                ViceProcess, "wait_for_exit", return_value=1
            ):
                render_wav(prg, wav, duration_seconds=0.01)
            args = list(mock_popen.call_args[0][0])
        assert "-warp" not in args
