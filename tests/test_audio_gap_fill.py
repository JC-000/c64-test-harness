"""AudioCapture zero-fills lost packets so the sample index stays a clock (#410).

Loopback through the real class, no device.  Each packet's PCM carries its own
sequence number plus one (never zero), so a zero packet in the WAV can only be
fill, and the WAV's packet order reads straight back.
"""
from __future__ import annotations

import logging
import socket
import struct
import time
import wave
from pathlib import Path

import pytest

from c64_test_harness.backends import u64_audio_capture as uac
from c64_test_harness.backends.render_wav_u64 import U64CaptureResult, _to_u64_result
from c64_test_harness.backends.u64_audio_capture import (
    AUDIO_FRAMES_PER_PACKET,
    AUDIO_PCM_BYTES_PER_PACKET,
    CHANNELS,
    EPHEMERAL_AUDIO_PORT,
    SAMPLE_WIDTH,
    U64_NTSC_AUDIO_RATE_HZ,
    AudioCapture,
    CaptureResult,
)

from audio_link_loss import (
    MAX_FILL_FRACTION,
    MEASURED_IDLE_LOSS_MAX,
    MEASURED_LOADED_LOSS_MIN,
    capture_usable,
    fill_near,
)

FILL = None  # marker for an all-zero packet in the decoded order


def _payload(seq: int, size: int = AUDIO_PCM_BYTES_PER_PACKET) -> bytes:
    return struct.pack("<H", (seq + 1) & 0xFFFF or 1) * (size // 2)


def _capture(seqs, tmp_path: Path, size: int = AUDIO_PCM_BYTES_PER_PACKET):
    cap = AudioCapture(
        port=EPHEMERAL_AUDIO_PORT, bind_addr="127.0.0.1",
        sample_rate=U64_NTSC_AUDIO_RATE_HZ, recv_buf_size=1 << 20,
    )
    cap.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            for i, s in enumerate(seqs):
                tx.sendto(struct.pack("<H", s) + _payload(s, size), ("127.0.0.1", cap.port))
                if i % 20 == 19:
                    time.sleep(0.005)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and cap.packets_received < len(seqs):
            time.sleep(0.01)
    finally:
        result = cap.stop(wav_path=tmp_path / "fill.wav")
    assert result.packets_received == len(seqs), "loopback lost packets"
    with wave.open(str(result.wav_path), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
    return result, pcm


def _order(pcm: bytes) -> list:
    step = AUDIO_PCM_BYTES_PER_PACKET
    assert len(pcm) % step == 0
    out = []
    for i in range(0, len(pcm), step):
        chunk = pcm[i:i + step]
        if chunk == bytes(step):
            out.append(FILL)
        else:
            out.append(struct.unpack_from("<H", chunk, 0)[0] - 1)
    return out


# --------------------------------------------------------------- the constant

def test_packet_size_is_the_documented_stream_format() -> None:
    """192 stereo 16-bit frames, 770 B on the wire with the 2 B sequence."""
    assert AUDIO_FRAMES_PER_PACKET == 192
    assert AUDIO_PCM_BYTES_PER_PACKET == AUDIO_FRAMES_PER_PACKET * CHANNELS * SAMPLE_WIDTH
    assert AUDIO_PCM_BYTES_PER_PACKET + uac._SEQ_HEADER_LEN == 770


# --------------------------------------------------------------- fill

def test_a_gap_is_filled_with_exactly_one_packet_of_silence_per_lost_packet(tmp_path) -> None:
    seqs = list(range(60)) + list(range(70, 100))
    result, pcm = _capture(seqs, tmp_path)
    assert result.packets_dropped == 10
    assert result.packets_filled == 10
    assert result.total_samples == 100 * AUDIO_FRAMES_PER_PACKET
    assert _order(pcm) == list(range(60)) + [FILL] * 10 + list(range(70, 100))
    assert result.filled_frame_ranges == ((60 * 192, 10 * 192),)
    assert result.fill_fraction == pytest.approx(0.1)
    assert result.time_base_intact is True


def test_a_late_packet_replaces_its_fill(tmp_path) -> None:
    seqs = list(range(51)) + [52, 53, 51] + list(range(54, 100))
    result, pcm = _capture(seqs, tmp_path)
    assert (result.packets_dropped, result.packets_filled) == (0, 0)
    assert _order(pcm) == list(range(100))
    assert result.filled_frame_ranges == ()
    assert result.fill_fraction == 0.0
    assert result.time_base_intact is True


def test_a_late_packet_inside_a_filled_burst_splits_the_range(tmp_path) -> None:
    seqs = list(range(50)) + list(range(55, 60)) + [52] + list(range(60, 100))
    result, pcm = _capture(seqs, tmp_path)
    assert (result.packets_dropped, result.packets_filled) == (4, 4)
    assert _order(pcm) == list(range(50)) + [FILL, FILL, 52, FILL, FILL] + list(range(55, 100))
    assert result.filled_frame_ranges == ((50 * 192, 2 * 192), (53 * 192, 2 * 192))
    assert result.time_base_intact is True


def test_a_gap_longer_than_the_reorder_window_is_still_filled_exactly(tmp_path) -> None:
    result, pcm = _capture([0, 1501, 1502], tmp_path)
    assert (result.packets_dropped, result.packets_filled) == (1500, 1500)
    assert result.total_samples == 1503 * AUDIO_FRAMES_PER_PACKET
    assert _order(pcm) == [0] + [FILL] * 1500 + [1501, 1502]
    assert result.filled_frame_ranges == ((192, 1500 * 192),)
    assert result.time_base_intact is True


def test_a_resync_breaks_the_time_base(tmp_path) -> None:
    """How many packets a restarted counter hid is unknown: not a clock."""
    seqs = list(range(3000, 3100)) + list(range(10))
    result, _ = _capture(seqs, tmp_path)
    assert result.sequence_resyncs == 1
    assert result.packets_filled == 0
    assert result.time_base_intact is False


def test_a_nonstandard_payload_makes_a_fill_untrustworthy(tmp_path) -> None:
    """Fill length is the documented 768 B; a stream of other sizes voids it."""
    result, _ = _capture([0, 1, 3, 4], tmp_path, size=400)
    assert result.nonstandard_payloads == 4
    assert result.packets_filled == 1
    assert result.time_base_intact is False


def test_a_nonstandard_payload_without_loss_keeps_the_time_base(tmp_path) -> None:
    result, _ = _capture([0, 1, 2, 3], tmp_path, size=400)
    assert result.nonstandard_payloads == 4
    assert result.time_base_intact is True


def test_stop_says_filled_not_discard_when_every_gap_was_filled(tmp_path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=uac.__name__):
        _capture([0, 1, 5, 6], tmp_path)
    assert "filled with silence" in caplog.text
    assert "Discard this capture" not in caplog.text


# --------------------------------------------------------------- result types

def test_a_constructed_result_without_fill_keeps_the_old_meaning() -> None:
    r = CaptureResult(Path("/dev/null"), 1.0, 47940, 47940, 100, 1)
    assert r.packets_filled == 0
    assert r.time_base_intact is False
    assert r.fill_fraction == 0.0


def test_u64_result_carries_fill_through() -> None:
    low = CaptureResult(
        Path("/dev/null"), 1.0, 47940, 19200, 90, 10,
        packets_reordered=2, packets_filled=10,
        filled_frame_ranges=((0, 1920),),
    )
    r = _to_u64_result(low)
    assert isinstance(r, U64CaptureResult)
    assert (r.packets_filled, r.packets_reordered) == (10, 2)
    assert r.filled_frame_ranges == ((0, 1920),)
    assert r.time_base_intact is True
    assert r.fill_fraction == pytest.approx(0.1)
    assert _to_u64_result(
        CaptureResult(Path("/dev/null"), 1.0, 47940, 19200, 90, 10, sequence_resyncs=1,
                      packets_filled=10)
    ).time_base_intact is False


# --------------------------------------------------------------- live tolerance

def test_fill_bound_sits_between_idle_and_loaded_link_loss() -> None:
    assert MEASURED_IDLE_LOSS_MAX < MAX_FILL_FRACTION < MEASURED_LOADED_LOSS_MIN
    assert MAX_FILL_FRACTION == pytest.approx(0.05)


def _r(dropped, filled, total, ranges=(), resyncs=0):
    return CaptureResult(Path("/dev/null"), 1.0, 47940, total, 100, dropped,
                         packets_filled=filled, filled_frame_ranges=ranges,
                         sequence_resyncs=resyncs)


def test_capture_usable_applies_both_conditions() -> None:
    assert capture_usable(_r(5, 5, 100 * 192)) is True          # 0.05, at the bound
    assert capture_usable(_r(6, 6, 100 * 192)) is False         # 0.06
    assert capture_usable(_r(1, 0, 100 * 192)) is False         # unfilled drop
    assert capture_usable(_r(0, 0, 100 * 192, resyncs=1)) is False


def test_fill_near_an_edge() -> None:
    r = _r(1, 1, 100 * 192, ranges=((1000, 192),))
    assert fill_near(r, 1000, 0) and fill_near(r, 1191, 0)
    assert not fill_near(r, 1192, 0) and not fill_near(r, 999, 0)
    assert fill_near(r, 990, 10) and fill_near(r, 1201, 10)
    assert not fill_near(r, 1202, 10)
