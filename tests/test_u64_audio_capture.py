"""Unit tests for u64_audio_capture module (AudioCapture, write_wav, CaptureResult)."""
from __future__ import annotations

import socket
import struct
import time
import wave
from pathlib import Path

import pytest

from c64_test_harness.backends.u64_audio_capture import (
    CHANNELS,
    DEFAULT_AUDIO_PORT,
    DEFAULT_SAMPLE_RATE,
    SAMPLE_WIDTH,
    AudioCapture,
    CaptureResult,
    write_wav,
)


# ---------------------------------------------------------------- helpers


def _reserve_port() -> tuple[int, socket.socket]:
    """Reserve a free UDP port and return ``(port, placeholder_socket)``.

    The placeholder socket stays bound to the port until the caller closes
    it — typically immediately before constructing/starting their own
    listener on the same port. Closing the placeholder before that point
    re-opens the TOCTOU window that this helper exists to close (see
    GitHub #91): with the placeholder closed, any other test in the same
    process can have the OS hand them this port before the caller's
    ``bind()`` lands.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1], s


def _send_test_packets(port: int, packets: list[tuple[int, bytes]]) -> None:
    """Send test packets [(seq, pcm_bytes), ...] to localhost:port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for seq, pcm in packets:
            header = struct.pack("<H", seq)
            sock.sendto(header + pcm, ("127.0.0.1", port))
            time.sleep(0.01)
    finally:
        sock.close()


def _make_pcm(n_frames: int = 100) -> bytes:
    """Create fake stereo 16-bit PCM data (n_frames * 4 bytes)."""
    return b"\x00\x01\x00\x02" * n_frames


# ---------------------------------------------------------------- write_wav


def test_write_wav_creates_valid_file(tmp_path: Path) -> None:
    pcm = _make_pcm(200)
    out = tmp_path / "test.wav"
    result = write_wav(out, pcm)
    assert result == out
    assert out.exists()

    with wave.open(str(out), "rb") as wf:
        assert wf.getnchannels() == CHANNELS
        assert wf.getsampwidth() == SAMPLE_WIDTH
        assert wf.getframerate() == DEFAULT_SAMPLE_RATE
        assert wf.getnframes() == 200


def test_write_wav_empty_data(tmp_path: Path) -> None:
    out = tmp_path / "empty.wav"
    write_wav(out, b"")
    assert out.exists()

    with wave.open(str(out), "rb") as wf:
        assert wf.getnframes() == 0
        assert wf.getnchannels() == CHANNELS
        assert wf.getsampwidth() == SAMPLE_WIDTH


# ---------------------------------------------------------------- CaptureResult


def test_capture_result_fields() -> None:
    cr = CaptureResult(
        wav_path=Path("/tmp/test.wav"),
        duration_seconds=2.5,
        sample_rate=48000,
        total_samples=120000,
        packets_received=500,
        packets_dropped=3,
    )
    assert cr.wav_path == Path("/tmp/test.wav")
    assert cr.duration_seconds == 2.5
    assert cr.sample_rate == 48000
    assert cr.total_samples == 120000
    assert cr.packets_received == 500
    assert cr.packets_dropped == 3


# ---------------------------------------------------------------- AudioCapture lifecycle


def test_audio_capture_start_stop_no_packets() -> None:
    port, _placeholder = _reserve_port()
    cap = AudioCapture(port=port)
    _placeholder.close()
    cap.start()
    result = cap.stop()
    assert result.packets_received == 0
    assert result.packets_dropped == 0
    assert result.duration_seconds == 0.0
    assert result.total_samples == 0


def test_audio_capture_double_start_raises() -> None:
    port, _placeholder = _reserve_port()
    cap = AudioCapture(port=port)
    _placeholder.close()
    cap.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            cap.start()
    finally:
        cap.stop()


def test_audio_capture_stop_not_started_raises() -> None:
    port, _placeholder = _reserve_port()
    cap = AudioCapture(port=port)
    _placeholder.close()
    with pytest.raises(RuntimeError, match="not started"):
        cap.stop()


# ---------------------------------------------------------------- packet reception


def test_audio_capture_receives_packets() -> None:
    port, _placeholder = _reserve_port()
    cap = AudioCapture(port=port)
    _placeholder.close()
    cap.start()
    try:
        pcm = _make_pcm(10)
        _send_test_packets(port, [(0, pcm), (1, pcm), (2, pcm)])
        time.sleep(0.1)
    finally:
        result = cap.stop()
    assert result.packets_received == 3
    assert result.packets_dropped == 0


def test_audio_capture_gap_detection() -> None:
    port, _placeholder = _reserve_port()
    cap = AudioCapture(port=port)
    _placeholder.close()
    cap.start()
    try:
        pcm = _make_pcm(10)
        # seq 0, 1, then skip to 5 -> gap of 3
        _send_test_packets(port, [(0, pcm), (1, pcm), (5, pcm)])
        time.sleep(0.1)
    finally:
        result = cap.stop()
    assert result.packets_received == 3
    assert result.packets_dropped == 3


def test_audio_capture_sequence_wrap() -> None:
    port, _placeholder = _reserve_port()
    cap = AudioCapture(port=port)
    _placeholder.close()
    cap.start()
    try:
        pcm = _make_pcm(10)
        _send_test_packets(port, [(0xFFFE, pcm), (0xFFFF, pcm), (0x0000, pcm)])
        time.sleep(0.1)
    finally:
        result = cap.stop()
    assert result.packets_received == 3
    assert result.packets_dropped == 0


def test_audio_capture_writes_wav(tmp_path: Path) -> None:
    port, _placeholder = _reserve_port()
    cap = AudioCapture(port=port)
    _placeholder.close()
    cap.start()
    try:
        pcm = _make_pcm(50)
        _send_test_packets(port, [(0, pcm), (1, pcm)])
        time.sleep(0.1)
    finally:
        wav_path = tmp_path / "capture.wav"
        result = cap.stop(wav_path=wav_path)
    assert wav_path.exists()
    assert result.wav_path == wav_path

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() > 0


# ---------------------------------------------------------------- properties


def test_audio_capture_is_capturing_property() -> None:
    port, _placeholder = _reserve_port()
    cap = AudioCapture(port=port)
    assert cap.is_capturing is False
    _placeholder.close()
    cap.start()
    assert cap.is_capturing is True
    cap.stop()
    assert cap.is_capturing is False


def test_audio_capture_packets_received_property() -> None:
    port, _placeholder = _reserve_port()
    cap = AudioCapture(port=port)
    _placeholder.close()
    cap.start()
    try:
        assert cap.packets_received == 0
        pcm = _make_pcm(10)
        _send_test_packets(port, [(0, pcm), (1, pcm)])
        time.sleep(0.1)
        assert cap.packets_received == 2
    finally:
        cap.stop()


# ---------------------------------------------------------------- constants


def test_constants() -> None:
    assert DEFAULT_AUDIO_PORT == 11001
    assert DEFAULT_SAMPLE_RATE == 48000
    assert CHANNELS == 2
    assert SAMPLE_WIDTH == 2


# ---------------------------------------------------------------- multicast


def test_audio_capture_multicast_join() -> None:
    """Verify multicast_group parameter is accepted at construction time."""
    port, _placeholder = _reserve_port()
    cap = AudioCapture(port=port, multicast_group="239.0.1.65")
    _placeholder.close()
    # Just verify construction succeeds and the attribute is stored
    assert cap._multicast_group == "239.0.1.65"


# ---------------------------------------------------------------- runt packet


def test_audio_capture_runt_packet_ignored() -> None:
    port, _placeholder = _reserve_port()
    cap = AudioCapture(port=port)
    _placeholder.close()
    cap.start()
    try:
        # Send a 1-byte packet (runt) and a 2-byte packet (just header, also runt)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(b"\x00", ("127.0.0.1", port))
            sock.sendto(b"\x00\x00", ("127.0.0.1", port))
            time.sleep(0.01)
            # Now send a valid packet
            pcm = _make_pcm(10)
            header = struct.pack("<H", 0)
            sock.sendto(header + pcm, ("127.0.0.1", port))
            time.sleep(0.1)
        finally:
            sock.close()
    finally:
        result = cap.stop()
    # Only the valid packet should be counted
    assert result.packets_received == 1
