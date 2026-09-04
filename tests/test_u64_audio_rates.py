"""U64 audio stream rate arithmetic and capture semantics (issues #195, #196).

Offline: no device, no network beyond localhost loopback in the one
capture test, and no VICE.

The rates are exact rationals derived from the NTSC colour carrier, and
the tests below re-derive them from ``Fc`` rather than restating the
constants, so a wrong constant fails rather than agreeing with itself.
"""
from __future__ import annotations

import logging
import wave
from fractions import Fraction
from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends import u64_audio_capture as uac
from c64_test_harness.backends.u64_audio_capture import (
    AudioCapture,
    CaptureResult,
    CHANNELS,
    DEFAULT_SAMPLE_RATE,
    NTSC_COLOR_CARRIER_HZ,
    NTSC_PHI2_HZ,
    PHI2_CYCLES_PER_AUDIO_SAMPLE,
    SAMPLE_WIDTH,
    U64_NTSC_AUDIO_RATE_HZ,
    coherent_block_cycles,
    write_wav,
)
from c64_test_harness.backends.render_wav import NTSC_CLOCK_HZ
from c64_test_harness.backends.render_wav_u64 import (
    U64CaptureResult,
    capture_sid_u64,
    capture_u64_audio,
)

_LOGGER = uac.__name__


# --------------------------------------------------------------------------- #
# The rates                                                                   #
# --------------------------------------------------------------------------- #
class TestExactRates:
    def test_colour_carrier(self) -> None:
        assert NTSC_COLOR_CARRIER_HZ == Fraction(315_000_000, 88)

    def test_phi2_is_fc_times_two_sevenths(self) -> None:
        assert NTSC_PHI2_HZ == NTSC_COLOR_CARRIER_HZ * Fraction(2, 7)
        assert NTSC_PHI2_HZ == Fraction(11_250_000, 11)

    def test_audio_rate_is_fc_times_three_two_twenty_fourths(self) -> None:
        assert U64_NTSC_AUDIO_RATE_HZ == NTSC_COLOR_CARRIER_HZ * Fraction(3, 224)
        assert U64_NTSC_AUDIO_RATE_HZ == Fraction(2_109_375, 44)

    def test_the_rate_is_not_48000(self) -> None:
        """The whole point of the issue."""
        assert U64_NTSC_AUDIO_RATE_HZ != DEFAULT_SAMPLE_RATE
        assert float(U64_NTSC_AUDIO_RATE_HZ) == pytest.approx(47940.3409, abs=1e-4)

    def test_the_nominal_rate_is_1244_ppm_wrong(self) -> None:
        error = (
            (DEFAULT_SAMPLE_RATE - U64_NTSC_AUDIO_RATE_HZ)
            / U64_NTSC_AUDIO_RATE_HZ
        )
        assert float(error) * 1e6 == pytest.approx(1244, abs=1)

    def test_that_is_about_75_ms_of_slip_per_minute(self) -> None:
        slip_seconds = 60 * (
            Fraction(1, DEFAULT_SAMPLE_RATE)
            - Fraction(1, 1) / U64_NTSC_AUDIO_RATE_HZ
        ) * U64_NTSC_AUDIO_RATE_HZ
        assert abs(float(slip_seconds)) * 1000 == pytest.approx(75, abs=2)

    def test_phi2_to_audio_locks_at_exactly_64_to_3(self) -> None:
        """``Fc`` cancels, so the ratio is crystal-independent."""
        assert PHI2_CYCLES_PER_AUDIO_SAMPLE == Fraction(64, 3)
        assert NTSC_PHI2_HZ / U64_NTSC_AUDIO_RATE_HZ == Fraction(64, 3)

    def test_the_lock_survives_an_arbitrary_crystal_error(self) -> None:
        """Not a tautology of the two constants: perturb ``Fc`` and the
        ratio must not move at all."""
        skewed_fc = NTSC_COLOR_CARRIER_HZ * Fraction(1_000_137, 1_000_000)
        phi2 = skewed_fc * Fraction(2, 7)
        audio = skewed_fc * Fraction(3, 224)
        assert phi2 / audio == Fraction(64, 3)

    def test_rounded_rates_give_a_7_ppm_arithmetic_error(self) -> None:
        """Why the constants are Fractions and not floats."""
        sloppy = 1022727.14 / 47940.0
        exact = float(Fraction(64, 3))
        assert sloppy == pytest.approx(21.333482, abs=1e-6)
        assert abs(sloppy - exact) / exact * 1e6 == pytest.approx(7, abs=1)

    def test_render_wav_ntsc_clock_is_the_same_quantity_rounded(self) -> None:
        """The two modules must not disagree about what phi2 is."""
        assert NTSC_CLOCK_HZ == round(NTSC_PHI2_HZ)

    def test_no_pal_audio_constant_is_published(self) -> None:
        """PAL does not lock and the rate is unmeasured here; publishing
        a guess would be worse than publishing nothing."""
        assert not [n for n in dir(uac) if "PAL" in n]


class TestCoherentBlockCycles:
    def test_three_samples_span_sixty_four_cycles(self) -> None:
        assert coherent_block_cycles(3) == 64

    def test_scales_exactly(self) -> None:
        assert coherent_block_cycles(300) == 6400
        assert coherent_block_cycles(48_000) == 1_024_000

    def test_result_is_an_exact_integer_number_of_cycles(self) -> None:
        for samples in (3, 9, 33, 999):
            assert (
                Fraction(coherent_block_cycles(samples))
                == samples * PHI2_CYCLES_PER_AUDIO_SAMPLE
            )

    @pytest.mark.parametrize("bad", [1, 2, 4, 100, 0, -3])
    def test_a_non_multiple_of_three_is_refused(self, bad) -> None:
        """A "nearly coherent" block is the failure this prevents."""
        with pytest.raises(ValueError, match="multiple of 3"):
            coherent_block_cycles(bad)

    def test_non_int_refused(self) -> None:
        with pytest.raises(TypeError):
            coherent_block_cycles(3.0)


# --------------------------------------------------------------------------- #
# The WAV header                                                              #
# --------------------------------------------------------------------------- #
class TestWavHeaderHonesty:
    def test_an_exact_rate_lands_in_the_header_rounded(self, tmp_path) -> None:
        path = write_wav(
            tmp_path / "a.wav", b"\x00\x00\x00\x00" * 10,
            sample_rate=U64_NTSC_AUDIO_RATE_HZ,
        )
        with wave.open(str(path)) as wf:
            assert wf.getframerate() == 47940

    def test_the_rounded_header_is_7_ppm_not_1244(self) -> None:
        residual = abs(47940 - U64_NTSC_AUDIO_RATE_HZ) / U64_NTSC_AUDIO_RATE_HZ
        assert float(residual) * 1e6 == pytest.approx(7.1, abs=0.5)

    def test_an_int_rate_is_unchanged(self, tmp_path) -> None:
        path = write_wav(tmp_path / "b.wav", b"\x00" * 4, sample_rate=44100)
        with wave.open(str(path)) as wf:
            assert wf.getframerate() == 44100

    def test_default_is_still_48000(self, tmp_path) -> None:
        """API stability: the public name and its value do not move."""
        assert DEFAULT_SAMPLE_RATE == 48000
        path = write_wav(tmp_path / "c.wav", b"\x00" * 4)
        with wave.open(str(path)) as wf:
            assert wf.getframerate() == 48000

    @pytest.mark.parametrize("bad", [0, -1, Fraction(-1, 2)])
    def test_a_non_positive_rate_is_refused(self, tmp_path, bad) -> None:
        with pytest.raises(ValueError, match="positive"):
            write_wav(tmp_path / "d.wav", b"\x00" * 4, sample_rate=bad)

    def test_a_nonsense_rate_type_is_refused(self, tmp_path) -> None:
        with pytest.raises(TypeError):
            write_wav(tmp_path / "e.wav", b"\x00" * 4, sample_rate="48000")


# --------------------------------------------------------------------------- #
# Dropped packets                                                             #
# --------------------------------------------------------------------------- #
def _result(dropped: int) -> CaptureResult:
    return CaptureResult(
        wav_path=tmp_marker,
        duration_seconds=1.0,
        sample_rate=47940,
        total_samples=47940,
        packets_received=100,
        packets_dropped=dropped,
    )


tmp_marker = __import__("pathlib").Path("/dev/null")


class TestDroppedPacketsBreakTheTimeBase:
    def test_a_clean_capture_is_usable(self) -> None:
        assert _result(0).time_base_intact is True

    def test_a_single_drop_invalidates_the_run(self) -> None:
        """Gaps are counted, never padded, so index stops being a clock."""
        assert _result(1).time_base_intact is False

    def test_u64_result_carries_the_same_check(self) -> None:
        assert U64CaptureResult(
            wav_path=tmp_marker,
            duration_seconds=1.0,
            sample_rate=47940,
            total_samples=1,
            packets_received=1,
            packets_dropped=2,
        ).time_base_intact is False

    def test_stop_warns_when_packets_were_lost(self, caplog, tmp_path) -> None:
        cap = AudioCapture(port=0, sample_rate=U64_NTSC_AUDIO_RATE_HZ)
        cap.start()
        try:
            # Inject a gap directly: the receive loop's own detection is
            # covered in test_u64_audio_capture.py; what is under test
            # here is what stop() does about it.
            with cap._lock:
                cap._packets_dropped = 3
                cap._packets_received = 7
                cap._pcm_chunks = [b"\x00\x01\x02\x03" * 8]
            with caplog.at_level(logging.WARNING, logger=_LOGGER):
                result = cap.stop(wav_path=tmp_path / "gap.wav")
        finally:
            if cap.is_capturing:
                cap.stop()
        assert result.packets_dropped == 3
        assert result.time_base_intact is False
        assert "Discard this capture" in caplog.text

    def test_no_drop_warning_on_a_clean_capture(self, caplog, tmp_path) -> None:
        cap = AudioCapture(port=0, sample_rate=U64_NTSC_AUDIO_RATE_HZ)
        cap.start()
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            result = cap.stop(wav_path=tmp_path / "clean.wav")
        assert result.time_base_intact is True
        assert "Discard this capture" not in caplog.text


# --------------------------------------------------------------------------- #
# AudioCapture rate handling                                                  #
# --------------------------------------------------------------------------- #
class TestAudioCaptureRate:
    def test_the_nominal_default_warns(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            AudioCapture(port=0)
        assert "1244 ppm" in caplog.text

    def test_the_exact_rate_does_not_warn(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            AudioCapture(port=0, sample_rate=U64_NTSC_AUDIO_RATE_HZ)
        assert caplog.text == ""

    def test_a_bad_rate_raises_at_construction_not_at_stop(self) -> None:
        """Failing after the capture has already happened would throw
        away the run."""
        with pytest.raises(ValueError):
            AudioCapture(port=0, sample_rate=0)

    def test_result_carries_both_the_header_and_the_exact_rate(
        self, tmp_path
    ) -> None:
        cap = AudioCapture(port=0, sample_rate=U64_NTSC_AUDIO_RATE_HZ)
        cap.start()
        result = cap.stop(wav_path=tmp_path / "r.wav")
        assert result.sample_rate == 47940
        assert result.sample_rate_exact == U64_NTSC_AUDIO_RATE_HZ

    def test_exact_rate_is_none_for_an_integer_rate(self, tmp_path) -> None:
        cap = AudioCapture(port=0, sample_rate=44100)
        cap.start()
        result = cap.stop(wav_path=tmp_path / "r.wav")
        assert result.sample_rate == 44100
        assert result.sample_rate_exact is None

    def test_duration_uses_the_exact_rate(self, tmp_path) -> None:
        """A block of exactly 3 samples is 3/47940.34 s, not 3/48000 s."""
        frames = 3
        pcm = b"\x00\x00\x00\x00" * frames
        cap = AudioCapture(port=0, sample_rate=U64_NTSC_AUDIO_RATE_HZ)
        cap.start()
        with cap._lock:
            cap._pcm_chunks = [pcm]
        result = cap.stop(wav_path=tmp_path / "d.wav")
        assert result.total_samples == frames
        assert result.duration_seconds == pytest.approx(
            float(Fraction(frames) / U64_NTSC_AUDIO_RATE_HZ), rel=1e-12
        )
        assert result.duration_seconds != pytest.approx(
            frames / 48000, rel=1e-9
        )


# --------------------------------------------------------------------------- #
# capture_sid_u64 no longer forces a reset (#196.1)                           #
# --------------------------------------------------------------------------- #
def _client() -> MagicMock:
    client = MagicMock()
    client.host = "127.0.0.1"
    return client


class _FakeSid:
    raw = b"PSID"
    name = "fake"


class TestCaptureSidResetControl:
    def test_reset_is_still_the_default(self, tmp_path) -> None:
        client = _client()
        out = tmp_path / "a.wav"
        capture_sid_u64(
            client, _FakeSid(), out, duration_seconds=0.0,
            settle_time=0.0, stream_destination="127.0.0.1:0",
            listen_port=0,
        )
        client.reset.assert_called_once()

    def test_reset_after_false_leaves_the_machine_alone(self, tmp_path) -> None:
        """The blocker: a host-driven run is destroyed by the reset."""
        client = _client()
        out = tmp_path / "b.wav"
        capture_sid_u64(
            client, _FakeSid(), out, duration_seconds=0.0,
            settle_time=0.0, stream_destination="127.0.0.1:0",
            listen_port=0, reset_after=False,
        )
        client.reset.assert_not_called()
        client.stream_audio_stop.assert_called_once()


class TestCaptureU64Audio:
    def test_it_never_resets_or_plays_anything(self, tmp_path) -> None:
        client = _client()
        with capture_u64_audio(
            client, tmp_path / "c.wav",
            listen_port=0, settle_time=0.0,
            stream_destination="127.0.0.1:0",
        ) as captured:
            assert captured == []
        client.reset.assert_not_called()
        client.sid_play.assert_not_called()
        client.stream_audio_start.assert_called_once_with("127.0.0.1:0")
        client.stream_audio_stop.assert_called_once()
        assert len(captured) == 1
        assert isinstance(captured[0], U64CaptureResult)

    def test_the_stream_is_stopped_and_the_wav_written_on_error(
        self, tmp_path
    ) -> None:
        client = _client()
        out = tmp_path / "d.wav"
        captured: list = []
        with pytest.raises(RuntimeError, match="boom"):
            with capture_u64_audio(
                client, out, listen_port=0, settle_time=0.0,
                stream_destination="127.0.0.1:0",
            ) as captured:
                raise RuntimeError("boom")
        client.stream_audio_stop.assert_called_once()
        assert out.exists()
        assert len(captured) == 1

    def test_no_wav_path_still_returns_statistics(self) -> None:
        client = _client()
        with capture_u64_audio(
            client, None, listen_port=0, settle_time=0.0,
            stream_destination="127.0.0.1:0",
        ) as captured:
            pass
        assert captured[0].packets_received == 0
