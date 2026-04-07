"""Tests for the batch WAV render feature."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.render_wav import (
    PAL_CLOCK_HZ,
    NTSC_CLOCK_HZ,
    RenderResult,
    render_wav,
)
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess


# ---------------------------------------------------------------------------
# Unit tests — ViceConfig sound fields and command line building
# ---------------------------------------------------------------------------


class TestViceConfigSoundFields:
    """Verify new ViceConfig fields have correct defaults and wire into CLI."""

    def test_default_sound_fields(self):
        cfg = ViceConfig()
        assert cfg.sounddev == ""
        assert cfg.soundarg == ""
        assert cfg.soundrate == 44100
        assert cfg.soundoutput == 1
        assert cfg.limit_cycles == 0
        assert cfg.env is None
        assert cfg.monitor is True

    def test_sound_fields_custom(self):
        cfg = ViceConfig(
            sounddev="wav",
            soundarg="/tmp/out.wav",
            soundrate=48000,
            soundoutput=2,
            limit_cycles=985248,
        )
        assert cfg.sounddev == "wav"
        assert cfg.soundarg == "/tmp/out.wav"
        assert cfg.soundrate == 48000
        assert cfg.soundoutput == 2
        assert cfg.limit_cycles == 985248

    @patch("subprocess.Popen")
    def test_sounddev_produces_correct_args(self, mock_popen):
        """When sounddev is set, the CLI should include sound device flags."""
        mock_popen.return_value = MagicMock()
        cfg = ViceConfig(
            sounddev="wav",
            soundarg="/tmp/test.wav",
            soundrate=22050,
            soundoutput=2,
            sound=False,
        )
        proc = ViceProcess(cfg)
        proc.start()

        args = mock_popen.call_args[0][0]
        assert "-sounddev" in args
        idx = args.index("-sounddev")
        assert args[idx + 1] == "wav"

        assert "-soundarg" in args
        idx = args.index("-soundarg")
        assert args[idx + 1] == "/tmp/test.wav"

        assert "-soundrate" in args
        idx = args.index("-soundrate")
        assert args[idx + 1] == "22050"

        assert "-soundoutput" in args
        idx = args.index("-soundoutput")
        assert args[idx + 1] == "2"

        # +sound should NOT be present when sounddev is set
        assert "+sound" not in args

        proc._proc = None  # prevent cleanup issues

    @patch("subprocess.Popen")
    def test_no_sounddev_emits_plus_sound(self, mock_popen):
        """Without sounddev and sound=False, +sound should be present."""
        mock_popen.return_value = MagicMock()
        cfg = ViceConfig(sound=False)
        proc = ViceProcess(cfg)
        proc.start()

        args = mock_popen.call_args[0][0]
        assert "+sound" in args
        assert "-sounddev" not in args
        proc._proc = None

    @patch("subprocess.Popen")
    def test_limit_cycles_produces_arg(self, mock_popen):
        """When limit_cycles > 0, -limitcycles should appear."""
        mock_popen.return_value = MagicMock()
        cfg = ViceConfig(limit_cycles=985248)
        proc = ViceProcess(cfg)
        proc.start()

        args = mock_popen.call_args[0][0]
        assert "-limitcycles" in args
        idx = args.index("-limitcycles")
        assert args[idx + 1] == "985248"
        proc._proc = None

    @patch("subprocess.Popen")
    def test_no_limit_cycles_no_arg(self, mock_popen):
        """When limit_cycles == 0, -limitcycles should not appear."""
        mock_popen.return_value = MagicMock()
        cfg = ViceConfig()
        proc = ViceProcess(cfg)
        proc.start()

        args = mock_popen.call_args[0][0]
        assert "-limitcycles" not in args
        proc._proc = None

    @patch("subprocess.Popen")
    def test_env_passed_to_popen(self, mock_popen):
        """When env is set, it should be passed to Popen."""
        mock_popen.return_value = MagicMock()
        env = {"SDL_VIDEODRIVER": "dummy", "PATH": "/usr/bin"}
        cfg = ViceConfig(env=env)
        proc = ViceProcess(cfg)
        proc.start()

        kwargs = mock_popen.call_args[1]
        assert kwargs["env"] == env
        proc._proc = None

    @patch("subprocess.Popen")
    def test_no_env_no_kwarg(self, mock_popen):
        """When env is None, env kwarg should not be passed to Popen."""
        mock_popen.return_value = MagicMock()
        cfg = ViceConfig()
        proc = ViceProcess(cfg)
        proc.start()

        kwargs = mock_popen.call_args[1]
        assert "env" not in kwargs
        proc._proc = None

    @patch("subprocess.Popen")
    def test_monitor_true_emits_binarymonitor(self, mock_popen):
        """When monitor=True (default), -binarymonitor should appear."""
        mock_popen.return_value = MagicMock()
        cfg = ViceConfig(monitor=True, port=6520)
        proc = ViceProcess(cfg)
        proc.start()

        args = mock_popen.call_args[0][0]
        assert "-binarymonitor" in args
        assert "-binarymonitoraddress" in args
        idx = args.index("-binarymonitoraddress")
        assert "6520" in args[idx + 1]
        proc._proc = None

    @patch("subprocess.Popen")
    def test_monitor_false_omits_binarymonitor(self, mock_popen):
        """When monitor=False, -binarymonitor should not appear."""
        mock_popen.return_value = MagicMock()
        cfg = ViceConfig(monitor=False)
        proc = ViceProcess(cfg)
        proc.start()

        args = mock_popen.call_args[0][0]
        assert "-binarymonitor" not in args
        assert "-binarymonitoraddress" not in args
        proc._proc = None


# ---------------------------------------------------------------------------
# Unit tests — wait_for_exit
# ---------------------------------------------------------------------------


class TestWaitForExit:
    """Test ViceProcess.wait_for_exit() method."""

    def test_wait_for_exit_returns_code(self):
        proc = ViceProcess(ViceConfig())
        mock_popen = MagicMock()
        mock_popen.wait.return_value = None
        mock_popen.returncode = 1
        proc._proc = mock_popen

        code = proc.wait_for_exit(timeout=10.0)
        assert code == 1
        mock_popen.wait.assert_called_once_with(timeout=10.0)
        # After wait, _proc should be cleared
        assert proc._proc is None

    def test_wait_for_exit_not_started_raises(self):
        proc = ViceProcess(ViceConfig())
        with pytest.raises(RuntimeError, match="not been started"):
            proc.wait_for_exit()

    def test_wait_for_exit_timeout_kills(self):
        proc = ViceProcess(ViceConfig())
        mock_popen = MagicMock()
        mock_popen.wait.side_effect = subprocess.TimeoutExpired(cmd="x64sc", timeout=5)
        mock_popen.terminate.return_value = None
        mock_popen.kill.return_value = None
        proc._proc = mock_popen

        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait_for_exit(timeout=5.0)
        # Process should be cleaned up
        assert proc._proc is None


# ---------------------------------------------------------------------------
# Unit tests — render_wav cycle computation and validation
# ---------------------------------------------------------------------------


class TestRenderWavCycles:
    """Test cycle computation and input validation in render_wav."""

    def test_pal_cycle_computation(self):
        """PAL: 1 second = 985248 cycles."""
        duration = 5.0
        expected = int(round(duration * PAL_CLOCK_HZ))
        assert expected == 4926240

    def test_ntsc_cycle_computation(self):
        """NTSC: 1 second = 1022727 cycles."""
        duration = 5.0
        expected = int(round(duration * NTSC_CLOCK_HZ))
        assert expected == 5113635

    def test_prg_not_found_raises(self, tmp_path):
        """render_wav should raise FileNotFoundError for missing .prg."""
        with pytest.raises(FileNotFoundError, match="PRG file not found"):
            render_wav(
                prg_path=tmp_path / "nonexistent.prg",
                out_wav=tmp_path / "out.wav",
                duration_seconds=1.0,
            )

    @patch("c64_test_harness.backends.render_wav.ViceProcess")
    def test_pal_cycles_passed_to_config(self, mock_vp_cls, tmp_path):
        """render_wav should compute PAL cycles and pass to ViceConfig."""
        prg = tmp_path / "test.prg"
        prg.write_bytes(b"\x01\x08")  # minimal .prg header
        wav = tmp_path / "out.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 40)  # fake WAV

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait_for_exit.return_value = 1
        mock_vp_cls.return_value = mock_proc

        result = render_wav(
            prg_path=prg,
            out_wav=wav,
            duration_seconds=2.0,
            pal=True,
        )

        assert result.cycles == int(round(2.0 * PAL_CLOCK_HZ))
        assert result.pid == 12345
        assert result.exit_code == 1
        assert result.wav_path == wav

        # Verify ViceConfig was created with correct limit_cycles
        config_arg = mock_vp_cls.call_args[0][0]
        assert config_arg.limit_cycles == int(round(2.0 * PAL_CLOCK_HZ))
        assert config_arg.sounddev == "wav"
        assert config_arg.ntsc is False  # PAL

    @patch("c64_test_harness.backends.render_wav.ViceProcess")
    def test_ntsc_cycles_passed_to_config(self, mock_vp_cls, tmp_path):
        """render_wav should compute NTSC cycles when pal=False."""
        prg = tmp_path / "test.prg"
        prg.write_bytes(b"\x01\x08")
        wav = tmp_path / "out.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 40)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait_for_exit.return_value = 1
        mock_vp_cls.return_value = mock_proc

        result = render_wav(
            prg_path=prg,
            out_wav=wav,
            duration_seconds=3.0,
            pal=False,
        )

        assert result.cycles == int(round(3.0 * NTSC_CLOCK_HZ))
        config_arg = mock_vp_cls.call_args[0][0]
        assert config_arg.ntsc is True  # NTSC mode

    @patch("c64_test_harness.backends.render_wav.ViceProcess")
    def test_missing_wav_raises_runtime_error(self, mock_vp_cls, tmp_path):
        """render_wav should raise if VICE exits but no WAV was created."""
        prg = tmp_path / "test.prg"
        prg.write_bytes(b"\x01\x08")
        wav = tmp_path / "out.wav"  # does not exist

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait_for_exit.return_value = 1
        mock_vp_cls.return_value = mock_proc

        with pytest.raises(RuntimeError, match="WAV file was not created"):
            render_wav(
                prg_path=prg,
                out_wav=wav,
                duration_seconds=1.0,
            )

    @patch("c64_test_harness.backends.render_wav.ViceProcess")
    def test_empty_wav_raises_runtime_error(self, mock_vp_cls, tmp_path):
        """render_wav should raise if VICE creates an empty WAV."""
        prg = tmp_path / "test.prg"
        prg.write_bytes(b"\x01\x08")
        wav = tmp_path / "out.wav"
        wav.write_bytes(b"")  # empty file

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait_for_exit.return_value = 1
        mock_vp_cls.return_value = mock_proc

        with pytest.raises(RuntimeError, match="empty WAV file"):
            render_wav(
                prg_path=prg,
                out_wav=wav,
                duration_seconds=1.0,
            )

    @patch("c64_test_harness.backends.render_wav.ViceProcess")
    def test_default_timeout_computation(self, mock_vp_cls, tmp_path):
        """Default timeout = max(30.0, duration * 1.5 + 20.0)."""
        prg = tmp_path / "test.prg"
        prg.write_bytes(b"\x01\x08")
        wav = tmp_path / "out.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 40)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait_for_exit.return_value = 1
        mock_vp_cls.return_value = mock_proc

        # Short duration -> timeout = 30.0 (the minimum)
        render_wav(prg_path=prg, out_wav=wav, duration_seconds=1.0)
        mock_proc.wait_for_exit.assert_called_with(timeout=30.0)

        # Longer duration -> timeout = duration * 1.5 + 20.0
        mock_proc.reset_mock()
        render_wav(prg_path=prg, out_wav=wav, duration_seconds=60.0)
        mock_proc.wait_for_exit.assert_called_with(timeout=110.0)

    @patch("c64_test_harness.backends.render_wav.ViceProcess")
    def test_sdl_videodriver_dummy_in_env(self, mock_vp_cls, tmp_path):
        """SDL_VIDEODRIVER=dummy should be set in the ViceConfig env."""
        prg = tmp_path / "test.prg"
        prg.write_bytes(b"\x01\x08")
        wav = tmp_path / "out.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 40)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait_for_exit.return_value = 1
        mock_vp_cls.return_value = mock_proc

        render_wav(prg_path=prg, out_wav=wav, duration_seconds=1.0)

        config_arg = mock_vp_cls.call_args[0][0]
        assert config_arg.env is not None
        assert config_arg.env.get("SDL_VIDEODRIVER") == "dummy"

    @patch("c64_test_harness.backends.render_wav.ViceProcess")
    def test_stereo_sets_soundoutput_2(self, mock_vp_cls, tmp_path):
        """mono=False should set soundoutput=2 in the ViceConfig."""
        prg = tmp_path / "test.prg"
        prg.write_bytes(b"\x01\x08")
        wav = tmp_path / "out.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 40)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait_for_exit.return_value = 1
        mock_vp_cls.return_value = mock_proc

        render_wav(prg_path=prg, out_wav=wav, duration_seconds=1.0, mono=False)

        config_arg = mock_vp_cls.call_args[0][0]
        assert config_arg.soundoutput == 2

    @patch("c64_test_harness.backends.render_wav.ViceProcess")
    def test_monitor_disabled_in_render(self, mock_vp_cls, tmp_path):
        """render_wav should set monitor=False on the ViceConfig."""
        prg = tmp_path / "test.prg"
        prg.write_bytes(b"\x01\x08")
        wav = tmp_path / "out.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 40)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait_for_exit.return_value = 1
        mock_vp_cls.return_value = mock_proc

        render_wav(prg_path=prg, out_wav=wav, duration_seconds=1.0)

        config_arg = mock_vp_cls.call_args[0][0]
        assert config_arg.monitor is False

    @patch("c64_test_harness.backends.render_wav.ViceProcess")
    def test_custom_base_config_inheritance(self, mock_vp_cls, tmp_path):
        """Passing a ViceConfig with custom executable and extra_args should preserve them."""
        prg = tmp_path / "test.prg"
        prg.write_bytes(b"\x01\x08")
        wav = tmp_path / "out.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 40)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait_for_exit.return_value = 1
        mock_vp_cls.return_value = mock_proc

        base = ViceConfig(executable="/opt/vice/bin/x64sc", extra_args=["-VICIIfilter", "0"])
        render_wav(prg_path=prg, out_wav=wav, duration_seconds=1.0, config=base)

        config_arg = mock_vp_cls.call_args[0][0]
        assert config_arg.executable == "/opt/vice/bin/x64sc"
        # Custom extra_args should be appended after the render_wav defaults
        assert "-VICIIfilter" in config_arg.extra_args
        assert "0" in config_arg.extra_args
        # render_wav's own args should also be present
        assert "+remotemonitor" in config_arg.extra_args
        # monitor=False means no +binarymonitor workaround needed
        assert config_arg.monitor is False

    @patch("c64_test_harness.backends.render_wav.ViceProcess")
    def test_stop_called_on_error(self, mock_vp_cls, tmp_path):
        """ViceProcess.stop() should be called when render fails."""
        prg = tmp_path / "test.prg"
        prg.write_bytes(b"\x01\x08")
        wav = tmp_path / "out.wav"  # missing - will cause RuntimeError

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait_for_exit.return_value = 1
        mock_vp_cls.return_value = mock_proc

        with pytest.raises(RuntimeError):
            render_wav(prg_path=prg, out_wav=wav, duration_seconds=1.0)
        mock_proc.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Integration test — requires x64sc on PATH
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("x64sc") is None,
    reason="x64sc not found",
)
class TestRenderWavIntegration:
    """Integration tests that actually run VICE."""

    def test_render_short_wav(self, tmp_path):
        """Render a very short WAV and verify the file is valid."""
        import struct
        import wave

        # Build a minimal C64 .prg that plays a tone on SID:
        #   *=$0801  (BASIC start)
        #   LDA #$0F : STA $D418  (max volume)
        #   LDA #$21 : STA $D404  (gate on, triangle)
        #   LDA #$1C : STA $D401  (freq hi)
        #   LDA #$00 : STA $D400  (freq lo)
        #   JMP *-2                (infinite loop)
        code = bytes([
            # BASIC stub: 10 SYS 2062
            0x01, 0x08,              # load address $0801
            0x0B, 0x08,              # next line pointer
            0x0A, 0x00,              # line number 10
            0x9E,                    # SYS token
            0x32, 0x30, 0x36, 0x32, # "2062"
            0x00,                    # end of line
            0x00, 0x00,              # end of BASIC
            # Machine code at $080E = 2062
            0xA9, 0x0F,              # LDA #$0F
            0x8D, 0x18, 0xD4,        # STA $D418 (volume)
            0xA9, 0x21,              # LDA #$21
            0x8D, 0x04, 0xD4,        # STA $D404 (waveform + gate)
            0xA9, 0x1C,              # LDA #$1C
            0x8D, 0x01, 0xD4,        # STA $D401 (freq hi)
            0xA9, 0x00,              # LDA #$00
            0x8D, 0x00, 0xD4,        # STA $D400 (freq lo)
            0x4C, 0x22, 0x08,        # JMP $0822 (loop back to last LDA)
        ])

        prg = tmp_path / "tone.prg"
        prg.write_bytes(code)

        wav_path = tmp_path / "output.wav"

        result = render_wav(
            prg_path=prg,
            out_wav=wav_path,
            duration_seconds=2.0,
            sample_rate=44100,
            mono=True,
            pal=True,
            timeout=60.0,
        )

        # Basic assertions
        assert result.wav_path == wav_path
        assert result.wav_path.exists()
        assert result.pid is not None
        assert result.exit_code in (0, 1)
        assert result.cycles == int(round(2.0 * PAL_CLOCK_HZ))

        # Verify it is a valid WAV file
        with wave.open(str(wav_path), "rb") as wf:
            assert wf.getframerate() == 44100
            assert wf.getnchannels() == 1
            n_frames = wf.getnframes()
            assert n_frames > 0
