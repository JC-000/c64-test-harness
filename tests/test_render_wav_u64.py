"""Unit tests for render_wav_u64 (capture_sid_u64 orchestrator)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from c64_test_harness.backends.render_wav_u64 import (
    U64CaptureResult,
    _detect_local_ip,
    capture_sid_u64,
)
from c64_test_harness.backends.u64_audio_capture import CaptureResult


# ---------------------------------------------------------------- helpers


def _fake_sid(name: str = "Test SID", raw: bytes = b"PSID-DATA") -> MagicMock:
    """Return a mock SidFile with .name and .raw attributes."""
    sid = MagicMock()
    sid.name = name
    sid.raw = raw
    return sid


def _fake_capture_result(wav_path: Path) -> CaptureResult:
    """Return a plausible CaptureResult for mocking AudioCapture.stop()."""
    return CaptureResult(
        wav_path=wav_path,
        duration_seconds=3.0,
        sample_rate=48000,
        total_samples=144000,
        packets_received=500,
        packets_dropped=0,
    )


# ---------------------------------------------------------------- U64CaptureResult


def test_u64_capture_result_fields() -> None:
    r = U64CaptureResult(
        wav_path=Path("/tmp/out.wav"),
        duration_seconds=5.0,
        sample_rate=48000,
        total_samples=240000,
        packets_received=1000,
        packets_dropped=2,
    )
    assert r.wav_path == Path("/tmp/out.wav")
    assert r.duration_seconds == 5.0
    assert r.sample_rate == 48000
    assert r.total_samples == 240000
    assert r.packets_received == 1000
    assert r.packets_dropped == 2


# ---------------------------------------------------------------- _detect_local_ip


@patch("c64_test_harness.backends.render_wav_u64.socket.socket")
def test_detect_local_ip(mock_socket_cls: MagicMock) -> None:
    mock_sock = MagicMock()
    mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
    mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)
    mock_sock.getsockname.return_value = ("192.168.1.42", 0)

    ip = _detect_local_ip("8.8.8.8")
    assert ip == "192.168.1.42"
    mock_sock.connect.assert_called_once_with(("8.8.8.8", 80))


# ---------------------------------------------------------------- call ordering


@patch("c64_test_harness.backends.render_wav_u64.time.sleep")
@patch("c64_test_harness.backends.render_wav_u64.AudioCapture")
@patch("c64_test_harness.backends.render_wav_u64._detect_local_ip", return_value="10.0.0.5")
def test_capture_sid_u64_calls_in_order(
    mock_detect: MagicMock,
    mock_cap_cls: MagicMock,
    mock_sleep: MagicMock,
    tmp_path: Path,
) -> None:
    wav = tmp_path / "out.wav"

    # Set up mocks
    mock_client = MagicMock()
    mock_client.host = "192.168.1.81"
    mock_cap = mock_cap_cls.return_value
    mock_cap.stop.return_value = _fake_capture_result(wav)

    # Create a real WAV file so validation passes
    _write_dummy_wav(wav)

    sid = _fake_sid()
    capture_sid_u64(mock_client, sid, wav, duration_seconds=3.0, settle_time=0.3)

    # Verify call order
    mock_cap.start.assert_called_once()
    mock_client.stream_audio_start.assert_called_once_with("10.0.0.5:11001")
    mock_client.sid_play.assert_called_once_with(sid.raw, songnr=0)
    # sleep called for settle_time and duration
    assert mock_sleep.call_count == 2
    mock_sleep.assert_any_call(0.3)
    mock_sleep.assert_any_call(3.0)
    mock_client.stream_audio_stop.assert_called_once()
    mock_cap.stop.assert_called_once_with(wav_path=wav)
    mock_client.reset.assert_called_once()


# ---------------------------------------------------------------- auto-detect destination


@patch("c64_test_harness.backends.render_wav_u64.time.sleep")
@patch("c64_test_harness.backends.render_wav_u64.AudioCapture")
@patch("c64_test_harness.backends.render_wav_u64._detect_local_ip", return_value="10.0.0.99")
def test_capture_sid_u64_auto_detect_destination(
    mock_detect: MagicMock,
    mock_cap_cls: MagicMock,
    mock_sleep: MagicMock,
    tmp_path: Path,
) -> None:
    wav = tmp_path / "out.wav"
    mock_client = MagicMock()
    mock_client.host = "192.168.1.81"
    mock_cap = mock_cap_cls.return_value
    mock_cap.stop.return_value = _fake_capture_result(wav)
    _write_dummy_wav(wav)

    capture_sid_u64(mock_client, _fake_sid(), wav, duration_seconds=1.0)

    mock_detect.assert_called_once_with("192.168.1.81")
    mock_client.stream_audio_start.assert_called_once_with("10.0.0.99:11001")


# ---------------------------------------------------------------- explicit destination


@patch("c64_test_harness.backends.render_wav_u64.time.sleep")
@patch("c64_test_harness.backends.render_wav_u64.AudioCapture")
@patch("c64_test_harness.backends.render_wav_u64._detect_local_ip")
def test_capture_sid_u64_explicit_destination(
    mock_detect: MagicMock,
    mock_cap_cls: MagicMock,
    mock_sleep: MagicMock,
    tmp_path: Path,
) -> None:
    wav = tmp_path / "out.wav"
    mock_client = MagicMock()
    mock_client.host = "192.168.1.81"
    mock_cap = mock_cap_cls.return_value
    mock_cap.stop.return_value = _fake_capture_result(wav)
    _write_dummy_wav(wav)

    capture_sid_u64(
        mock_client, _fake_sid(), wav,
        duration_seconds=1.0,
        stream_destination="10.0.0.42:9999",
    )

    mock_detect.assert_not_called()
    mock_client.stream_audio_start.assert_called_once_with("10.0.0.42:9999")


# ---------------------------------------------------------------- cleanup on error


@patch("c64_test_harness.backends.render_wav_u64.time.sleep")
@patch("c64_test_harness.backends.render_wav_u64.AudioCapture")
@patch("c64_test_harness.backends.render_wav_u64._detect_local_ip", return_value="10.0.0.5")
def test_capture_sid_u64_cleanup_on_error(
    mock_detect: MagicMock,
    mock_cap_cls: MagicMock,
    mock_sleep: MagicMock,
    tmp_path: Path,
) -> None:
    wav = tmp_path / "out.wav"
    mock_client = MagicMock()
    mock_client.host = "192.168.1.81"
    mock_client.sid_play.side_effect = RuntimeError("SID play failed")
    mock_cap = mock_cap_cls.return_value
    mock_cap.stop.return_value = _fake_capture_result(wav)
    _write_dummy_wav(wav)

    with pytest.raises(RuntimeError, match="SID play failed"):
        capture_sid_u64(mock_client, _fake_sid(), wav, duration_seconds=1.0)

    # Cleanup should still happen
    mock_client.stream_audio_stop.assert_called_once()
    mock_cap.stop.assert_called_once()
    mock_client.reset.assert_called_once()


# ---------------------------------------------------------------- output validation


@patch("c64_test_harness.backends.render_wav_u64.time.sleep")
@patch("c64_test_harness.backends.render_wav_u64.AudioCapture")
@patch("c64_test_harness.backends.render_wav_u64._detect_local_ip", return_value="10.0.0.5")
def test_capture_sid_u64_validates_output(
    mock_detect: MagicMock,
    mock_cap_cls: MagicMock,
    mock_sleep: MagicMock,
    tmp_path: Path,
) -> None:
    wav = tmp_path / "missing.wav"  # does not exist
    mock_client = MagicMock()
    mock_client.host = "192.168.1.81"
    mock_cap = mock_cap_cls.return_value
    mock_cap.stop.return_value = _fake_capture_result(wav)
    # Don't create the WAV file -- validation should fail

    with pytest.raises(RuntimeError, match="WAV file was not created"):
        capture_sid_u64(mock_client, _fake_sid(), wav, duration_seconds=1.0)


@patch("c64_test_harness.backends.render_wav_u64.time.sleep")
@patch("c64_test_harness.backends.render_wav_u64.AudioCapture")
@patch("c64_test_harness.backends.render_wav_u64._detect_local_ip", return_value="10.0.0.5")
def test_capture_sid_u64_validates_empty_output(
    mock_detect: MagicMock,
    mock_cap_cls: MagicMock,
    mock_sleep: MagicMock,
    tmp_path: Path,
) -> None:
    wav = tmp_path / "empty.wav"
    wav.write_bytes(b"")  # empty file
    mock_client = MagicMock()
    mock_client.host = "192.168.1.81"
    mock_cap = mock_cap_cls.return_value
    mock_cap.stop.return_value = _fake_capture_result(wav)

    with pytest.raises(RuntimeError, match="empty"):
        capture_sid_u64(mock_client, _fake_sid(), wav, duration_seconds=1.0)


# ---------------------------------------------------------------- custom params


@patch("c64_test_harness.backends.render_wav_u64.time.sleep")
@patch("c64_test_harness.backends.render_wav_u64.AudioCapture")
@patch("c64_test_harness.backends.render_wav_u64._detect_local_ip", return_value="10.0.0.5")
def test_capture_sid_u64_custom_params(
    mock_detect: MagicMock,
    mock_cap_cls: MagicMock,
    mock_sleep: MagicMock,
    tmp_path: Path,
) -> None:
    wav = tmp_path / "out.wav"
    mock_client = MagicMock()
    mock_client.host = "192.168.1.81"
    mock_cap = mock_cap_cls.return_value
    mock_cap.stop.return_value = _fake_capture_result(wav)
    _write_dummy_wav(wav)

    capture_sid_u64(
        mock_client,
        _fake_sid(),
        wav,
        duration_seconds=5.0,
        sample_rate=44100,
        listen_port=22222,
        settle_time=1.0,
    )

    # Verify AudioCapture was constructed with custom params
    mock_cap_cls.assert_called_once_with(
        port=22222,
        sample_rate=44100,
        bind_addr="",
    )

    # Verify sleep durations
    mock_sleep.assert_any_call(1.0)   # settle_time
    mock_sleep.assert_any_call(5.0)   # duration

    # Verify stream destination uses custom port
    mock_client.stream_audio_start.assert_called_once_with("10.0.0.5:22222")


# ---------------------------------------------------------------- helper


def _write_dummy_wav(path: Path) -> None:
    """Write a minimal valid WAV file for validation to pass."""
    import wave as _wave
    with _wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(b"\x00\x01" * 100)
