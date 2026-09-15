"""Sequence accounting of the U64 audio and debug stream receivers (#430).

Both receivers used to set ``_last_seq`` to a *backward* sequence number, so
the packet after a late or duplicated one was charged as a forward gap as
well: an adjacent swap read as 2 dropped, a duplicate as 1, and a packet
*d* positions late as *d* + 1.  ``AudioCapture`` also appended the late PCM
in arrival order, so the sample index was shuffled even when nothing was
lost.

Every case here goes over loopback through the real classes, with every
datagram received (asserted), so the true missing count is known exactly.
No device.  The clean and one-gap streams are the controls: a fix that
stops counting real gaps fails them.
"""
from __future__ import annotations

import socket
import struct
import time
import wave
from pathlib import Path

import pytest

from c64_test_harness.backends import _stream_seq
from c64_test_harness.backends.u64_audio_capture import (
    EPHEMERAL_AUDIO_PORT,
    U64_NTSC_AUDIO_RATE_HZ,
    AudioCapture,
)
from c64_test_harness.backends.u64_debug_capture import (
    ENTRIES_PER_PACKET,
    ENTRY_SIZE,
    DebugCapture,
)

#: PCM bytes per audio packet; each packet's frames carry its own sequence
#: number so the WAV's packet order can be read back.
_AUDIO_PAYLOAD_LEN = 768


def _audio_payload(seq: int) -> bytes:
    return struct.pack("<H", seq) * (_AUDIO_PAYLOAD_LEN // 2)


def _debug_payload(seq: int) -> bytes:
    return struct.pack("<I", seq) * ENTRIES_PER_PACKET


def _send(dest, datagrams: list[bytes], received) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
        for i, d in enumerate(datagrams):
            tx.sendto(d, dest)
            if i % 20 == 19:
                time.sleep(0.005)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and received() < len(datagrams):
        time.sleep(0.01)


def _audio(seqs: list[int], tmp_path: Path):
    """Capture *seqs* with AudioCapture; returns (result, packet order in WAV)."""
    cap = AudioCapture(
        port=EPHEMERAL_AUDIO_PORT, bind_addr="127.0.0.1",
        sample_rate=U64_NTSC_AUDIO_RATE_HZ, recv_buf_size=1 << 20,
    )
    cap.start()
    try:
        _send(
            ("127.0.0.1", cap.port),
            [struct.pack("<H", s) + _audio_payload(s) for s in seqs],
            lambda: cap.packets_received,
        )
    finally:
        result = cap.stop(wav_path=tmp_path / "seq.wav")
    assert result.packets_received == len(seqs), "loopback lost packets"
    with wave.open(str(result.wav_path), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
    assert len(pcm) % _AUDIO_PAYLOAD_LEN == 0
    order = [
        struct.unpack_from("<H", pcm, i)[0]
        for i in range(0, len(pcm), _AUDIO_PAYLOAD_LEN)
    ]
    return result, order


def _debug(seqs: list[int]):
    """Capture *seqs* with DebugCapture; returns (result, packet order in trace)."""
    cap = DebugCapture(port=0, bind_addr="127.0.0.1", recv_buf_size=1 << 20)
    cap.start()
    try:
        assert cap._sock is not None
        _send(
            cap._sock.getsockname(),
            [struct.pack("<HH", s, 0) + _debug_payload(s) for s in seqs],
            lambda: cap.packets_received,
        )
    finally:
        result = cap.stop()
    assert result.packets_received == len(seqs), "loopback lost packets"
    order = [c.raw for c in result.trace[::ENTRIES_PER_PACKET]]
    assert result.total_cycles == len(order) * ENTRIES_PER_PACKET
    return result, order


_R = list(range(100))

#: name -> (stream, true dropped, backward events, packet order once placed)
CASES = {
    "clean": (_R, 0, 0, _R),
    "one_gap": (list(range(60)) + list(range(70, 100)), 10, 0,
                list(range(60)) + list(range(70, 100))),
    "duplicate": (list(range(50)) + [48] + list(range(50, 100)), 0, 1, _R),
    "adjacent_swap": (list(range(51)) + [52, 51] + list(range(53, 100)), 0, 1, _R),
    "late_by_4": (list(range(51)) + [52, 53, 54, 55, 51] + list(range(56, 100)),
                  0, 1, _R),
    "swap_across_wrap": ([0xFFFD, 0xFFFE, 0, 0xFFFF, 1, 2], 0, 1,
                         [0xFFFD, 0xFFFE, 0xFFFF, 0, 1, 2]),
    "late_packet_arrives_last": ([0, 1, 3, 2], 0, 1, [0, 1, 2, 3]),
    "gap_then_late_fill_of_part": (
        list(range(50)) + list(range(55, 60)) + [52] + list(range(60, 100)),
        4, 1, None),
}


@pytest.mark.parametrize("name", list(CASES))
def test_audio_capture_counts_only_true_loss(name: str, tmp_path: Path) -> None:
    seqs, dropped, backward, placed = CASES[name]
    result, order = _audio(seqs, tmp_path)
    assert result.packets_dropped == dropped
    assert result.packets_reordered == backward
    assert result.time_base_intact is (dropped == 0)
    if placed is not None:
        # A late packet lands in its own slot; a duplicate adds no samples.
        assert order == placed


def test_audio_late_packet_after_gap_fills_its_own_slot(tmp_path: Path) -> None:
    """55..59 arrived before 52: 52 goes back between 51 and 55, not after 59."""
    seqs, *_ = CASES["gap_then_late_fill_of_part"]
    _, order = _audio(seqs, tmp_path)
    assert order == list(range(50)) + [52] + list(range(55, 100))


@pytest.mark.parametrize("name", list(CASES))
def test_debug_capture_counts_only_true_loss(name: str) -> None:
    seqs, dropped, backward, _ = CASES[name]
    result, order = _debug(seqs)
    assert result.packets_dropped == dropped
    assert result.packets_reordered == backward


def test_debug_capture_discards_a_duplicate_payload() -> None:
    """A duplicated datagram would count its 360 bus cycles twice."""
    seqs, *_ = CASES["duplicate"]
    _, order = _debug(seqs)
    assert order == _R


def test_debug_capture_keeps_a_late_payload_in_arrival_order() -> None:
    """Debug has no slots to fill: a late packet's cycles are kept, appended."""
    seqs, *_ = CASES["adjacent_swap"]
    _, order = _debug(seqs)
    assert order == seqs


def test_a_far_backward_jump_resyncs_rather_than_discarding(tmp_path: Path) -> None:
    """A stream that restarts its counter must not have every later packet
    discarded as a duplicate: past the reorder window a backward step is a
    resync (one backward event, nothing dropped, all payload kept)."""
    seqs = list(range(3000, 3100)) + list(range(10))
    result, order = _audio(seqs, tmp_path)
    assert (result.packets_dropped, result.packets_reordered) == (0, 1)
    assert order == seqs
    dresult, dorder = _debug(seqs)
    assert (dresult.packets_dropped, dresult.packets_reordered) == (0, 1)
    assert dorder == seqs


def test_a_late_packet_inside_the_window_after_a_long_gap(tmp_path: Path) -> None:
    """0, then 1501: 1500 missing.  1000 arriving late is inside the window
    (age 501) and un-counts one; the gap keeps the other 1499."""
    result, _ = _audio([0, 1501, 1000, 1502], tmp_path)
    assert (result.packets_dropped, result.packets_reordered) == (1499, 1)


def test_tracker_forgets_missing_numbers_older_than_the_window() -> None:
    """1 goes missing, then the stream runs on to 1030 (age of 1 = 1029).

    Past the window 1 is no longer remembered, so its arrival is a resync,
    not a late fill: the tracker's memory is bounded, and a wrapped 16-bit
    counter cannot later match a stale entry."""
    t = _stream_seq.SequenceTracker()
    assert t.observe(0).kind == _stream_seq.NEXT
    assert t.observe(2).kind == _stream_seq.GAP
    for s in range(3, 1031):
        t.observe(s)
    assert t.observe(1).kind == _stream_seq.RESYNC
    assert (t.dropped, t.reordered) == (1, 1)


def test_tracker_remembers_a_missing_number_just_inside_the_window() -> None:
    t = _stream_seq.SequenceTracker()
    t.observe(0)
    t.observe(2)
    for s in range(3, 1025):  # age of 1 is 1023 < 1024
        t.observe(s)
    assert t.observe(1).kind == _stream_seq.LATE
    assert (t.dropped, t.reordered) == (0, 1)


def test_tracker_forgets_a_missing_number_at_exactly_the_window() -> None:
    """Boundary: age of 1 is exactly 1024 when it arrives -- forgotten."""
    t = _stream_seq.SequenceTracker()
    t.observe(0)
    t.observe(2)
    for s in range(3, 1026):  # highest 1025, age of 1 = 1024
        t.observe(s)
    assert t.observe(1).kind == _stream_seq.RESYNC
