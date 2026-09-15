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
    # A nonzero marker word beside the sequence, so an all-zero packet in the
    # WAV can only be gap fill (#410), whatever the sequence number.
    return struct.pack("<HH", seq, 0xA5A5) * (_AUDIO_PAYLOAD_LEN // 4)


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
        None if pcm[i:i + _AUDIO_PAYLOAD_LEN] == bytes(_AUDIO_PAYLOAD_LEN)
        else struct.unpack_from("<H", pcm, i)[0]
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
                list(range(60)) + [None] * 10 + list(range(70, 100))),
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
    # Every drop is zero-filled (#410), so the time base survives all of these.
    assert result.time_base_intact is True
    if placed is not None:
        # A late packet lands in its own slot; a duplicate adds no samples.
        assert order == placed


def test_audio_late_packet_after_gap_fills_its_own_slot(tmp_path: Path) -> None:
    """55..59 arrived before 52: 52 goes back between 51 and 55, not after 59."""
    seqs, *_ = CASES["gap_then_late_fill_of_part"]
    _, order = _audio(seqs, tmp_path)
    assert order == list(range(50)) + [None, None, 52, None, None] + list(range(55, 100))


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


# ----------------------------------------------------------------------------
# #443 review round 1: restarts, held duplicates, over-late packets, memory.
# ----------------------------------------------------------------------------

_A, _B = 0xA5A5, 0x5A5A


def _audio_raw(datagrams: list[tuple[int, bytes]], tmp_path: Path):
    """Capture explicit (seq, pcm) datagrams; returns (result, raw PCM)."""
    cap = AudioCapture(
        port=EPHEMERAL_AUDIO_PORT, bind_addr="127.0.0.1",
        sample_rate=U64_NTSC_AUDIO_RATE_HZ, recv_buf_size=1 << 20,
    )
    cap.start()
    try:
        _send(
            ("127.0.0.1", cap.port),
            [struct.pack("<H", s) + p for s, p in datagrams],
            lambda: cap.packets_received,
        )
    finally:
        result = cap.stop(wav_path=tmp_path / "raw.wav")
    assert result.packets_received == len(datagrams), "loopback lost packets"
    with wave.open(str(result.wav_path), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
    return result, pcm


def _debug_raw(datagrams: list[tuple[int, bytes]]):
    cap = DebugCapture(port=0, bind_addr="127.0.0.1", recv_buf_size=1 << 20)
    cap.start()
    try:
        assert cap._sock is not None
        _send(
            cap._sock.getsockname(),
            [struct.pack("<HH", s, 0) + p for s, p in datagrams],
            lambda: cap.packets_received,
        )
    finally:
        result = cap.stop()
    assert result.packets_received == len(datagrams), "loopback lost packets"
    return result


def _apcm(seq: int, marker: int) -> bytes:
    return struct.pack("<HH", seq, marker) * (_AUDIO_PAYLOAD_LEN // 4)


def _dpay(seq: int, marker: int) -> bytes:
    return struct.pack("<I", seq | (marker << 16)) * ENTRIES_PER_PACKET


def _apackets(pcm: bytes) -> list[tuple[int, int]]:
    return [
        struct.unpack_from("<HH", pcm, i)
        for i in range(0, len(pcm), _AUDIO_PAYLOAD_LEN)
    ]


def test_restart_below_received_numbers_keeps_every_packet(tmp_path: Path) -> None:
    """0..500 then a restarted 0..599 with new content: all 1101 kept, in
    arrival order, one resync, nothing dropped (head before this discarded
    the new stream's first 501 as duplicates)."""
    stream = [(s, _A) for s in range(501)] + [(s, _B) for s in range(600)]
    result, pcm = _audio_raw([(s, _apcm(s, m)) for s, m in stream], tmp_path)
    assert _apackets(pcm) == stream
    assert (result.packets_dropped, result.packets_reordered,
            result.sequence_resyncs) == (0, 1, 1)
    d = _debug_raw([(s, _dpay(s, m)) for s, m in stream])
    assert [c.raw for c in d.trace[::ENTRIES_PER_PACKET]] == [
        s | (m << 16) for s, m in stream
    ]
    assert (d.packets_dropped, d.packets_reordered, d.sequence_resyncs) == (0, 1, 1)


def test_silent_restart_is_recognised_and_loses_nothing(tmp_path: Path) -> None:
    """Identical (all-zero) payloads defeat the digest; the continuing run
    does not: every one of the 1101 packets is kept and the time base holds."""
    zero_a = bytes(_AUDIO_PAYLOAD_LEN)
    seqs = list(range(501)) + list(range(600))
    result, pcm = _audio_raw([(s, zero_a) for s in seqs], tmp_path)
    assert len(pcm) == 1101 * _AUDIO_PAYLOAD_LEN
    assert result.packets_dropped == 0
    assert result.sequence_resyncs == 1
    # Every packet is kept, but a restart hides how many packets the device
    # never sent between the two runs, so the index is not a clock across it
    # (#410: a resync breaks time_base_intact).
    assert result.time_base_intact is False
    zero_d = bytes(ENTRIES_PER_PACKET * ENTRY_SIZE)
    d = _debug_raw([(s, zero_d) for s in seqs])
    assert d.total_cycles == 1101 * ENTRIES_PER_PACKET
    assert (d.packets_dropped, d.sequence_resyncs) == (0, 1)


def test_forward_loss_near_64600_resyncs_and_keeps_packets(tmp_path: Path) -> None:
    """999 then 64: a forward jump of 64,600 reads as 935 back, over numbers
    already received.  New content there means a resync, not duplicates."""
    after = [((1000 + 64600 + i) & 0xFFFF, _B) for i in range(36)]
    assert after[0][0] == 64
    stream = [(s, _A) for s in range(1000)] + after
    result, pcm = _audio_raw([(s, _apcm(s, m)) for s, m in stream], tmp_path)
    assert _apackets(pcm) == stream
    assert (result.packets_dropped, result.sequence_resyncs) == (0, 1)


def test_a_duplicate_pair_is_not_a_restart(tmp_path: Path) -> None:
    """48 and 49 re-sent after 50, then 51: two duplicates, no resync, no
    phantom gap charged to 51."""
    seqs = list(range(51)) + [48, 49] + list(range(51, 100))
    result, pcm = _audio_raw([(s, _apcm(s, _A)) for s in seqs], tmp_path)
    assert [s for s, _ in _apackets(pcm)] == list(range(100))
    assert (result.packets_dropped, result.packets_reordered,
            result.sequence_resyncs) == (0, 2, 0)
    d = _debug_raw([(s, _dpay(s, _A)) for s in seqs])
    assert (d.packets_dropped, d.packets_reordered, d.sequence_resyncs) == (0, 2, 0)
    assert d.total_cycles == 100 * ENTRIES_PER_PACKET


@pytest.mark.parametrize("name", list(CASES))
def test_none_of_the_reorder_cases_resyncs(name: str, tmp_path: Path) -> None:
    seqs, *_ = CASES[name]
    result, _ = _audio(seqs, tmp_path)
    assert result.sequence_resyncs == 0


def test_duplicates_still_held_at_stop_are_discarded(tmp_path: Path, caplog) -> None:
    """A stream ending on re-sent packets: flushed as duplicates, counted,
    and said so at stop()."""
    seqs = list(range(10)) + [5, 6]
    with caplog.at_level("WARNING", logger="c64_test_harness.backends.u64_audio_capture"):
        result, pcm = _audio_raw([(s, _apcm(s, _A)) for s in seqs], tmp_path)
    assert [s for s, _ in _apackets(pcm)] == list(range(10))
    assert (result.packets_dropped, result.packets_reordered,
            result.sequence_resyncs) == (0, 2, 0)
    assert "stopped with 2 re-sent packet(s) undecided" in caplog.text


def test_a_duplicate_of_a_late_packet_is_a_duplicate(tmp_path: Path) -> None:
    """52, then 51 late, then 51 again: the second 51 was actually received
    (late), so it is a duplicate -- no resync, no phantom gap for 53."""
    seqs = list(range(51)) + [52, 51, 51] + list(range(53, 100))
    result, pcm = _audio_raw([(s, _apcm(s, _A)) for s in seqs], tmp_path)
    assert [s for s, _ in _apackets(pcm)] == list(range(100))
    assert (result.packets_dropped, result.packets_reordered,
            result.sequence_resyncs) == (0, 2, 0)


def test_a_duplicate_of_the_newest_packet_then_in_order_traffic(tmp_path: Path) -> None:
    """50 re-sent right after 50, then 51: 51 is ahead of the highest number,
    so it does not continue a restart -- the held 50 is a duplicate."""
    seqs = list(range(51)) + [50] + list(range(51, 100))
    result, pcm = _audio_raw([(s, _apcm(s, _A)) for s in seqs], tmp_path)
    assert [s for s, _ in _apackets(pcm)] == list(range(100))
    assert (result.packets_dropped, result.packets_reordered,
            result.sequence_resyncs) == (0, 1, 0)


def test_duplicate_arriving_last_is_counted(tmp_path: Path) -> None:
    result, pcm = _audio_raw([(s, _apcm(s, _A)) for s in [0, 1, 2, 1]], tmp_path)
    assert [s for s, _ in _apackets(pcm)] == [0, 1, 2]
    assert (result.packets_dropped, result.packets_reordered) == (0, 1)


def test_an_over_late_packet_resyncs_and_overcounts_as_documented(tmp_path: Path) -> None:
    """1 arrives 1098 positions late, past the window: it is appended where
    it arrived, the position moves back to 1, and 1100 is charged a gap of
    1098.  The documented residual overcount, pinned so it cannot change
    silently."""
    seqs = [0] + list(range(2, 1100)) + [1] + list(range(1100, 1110))
    result, pcm = _audio_raw([(s, _apcm(s, _A)) for s in seqs], tmp_path)
    fill = (0, 0)  # an all-zero packet: gap fill (#410)
    assert _apackets(pcm) == (
        [(0, _A), fill] + [(s, _A) for s in range(2, 1100)] + [(1, _A)]
        + [fill] * 1098 + [(s, _A) for s in range(1100, 1110)]
    )
    assert (result.packets_dropped, result.packets_reordered,
            result.sequence_resyncs) == (1099, 1, 1)
    assert result.packets_filled == 1099


def test_tracker_memory_is_bounded_by_the_window() -> None:
    t = _stream_seq.SequenceTracker()
    for s in range(0, 8000, 2):  # every other packet lost
        t.observe(s, s)
    assert len(t._missing) < t.window
    assert len(t._received) <= t.window
    for s in range(7001, 8000, 2):  # the losses turn up late
        t.observe(s, s)
    assert len(t._missing) < t.window
    assert len(t._received) <= t.window
    for s in range(8000, 12000):
        t.observe(s, s)
    assert len(t._missing) == 0
    assert len(t._received) <= t.window


def test_tracker_counts_a_forward_gap_of_20000() -> None:
    t = _stream_seq.SequenceTracker()
    t.observe(0)
    assert t.observe(20001).kind == _stream_seq.GAP
    assert (t.dropped, t.resyncs) == (20000, 0)


def test_tracker_held_cap() -> None:
    """Up to MAX_HELD_DUPLICATES identical re-sends stay duplicates; one more
    continuing the run is a restart that re-admits every held token."""
    cap = _stream_seq.MAX_HELD_DUPLICATES
    t = _stream_seq.SequenceTracker()
    for s in range(20):
        t.observe(s, "x", s)
    for s in range(cap):
        assert t.observe(s, "x", ("again", s)).kind == _stream_seq.HELD
    ev = t.observe(20, "x", 20)
    assert (ev.kind, ev.discarded_held, t.resyncs) == (_stream_seq.NEXT, cap, 0)

    t = _stream_seq.SequenceTracker()
    for s in range(20):
        t.observe(s, "x", s)
    for s in range(cap):
        t.observe(s, "x", ("again", s))
    ev = t.observe(cap, "x", ("again", cap))
    assert ev.kind == _stream_seq.RESYNC
    assert ev.readmit == tuple(("again", s) for s in range(cap))
    assert t.resyncs == 1


def test_tracker_restart_with_new_content_after_a_hold_readmits() -> None:
    t = _stream_seq.SequenceTracker()
    for s in range(20):
        t.observe(s, "old", s)
    assert t.observe(0, "old", "h0").kind == _stream_seq.HELD
    ev = t.observe(1, "new", "n1")
    assert (ev.kind, ev.readmit, t.resyncs) == (_stream_seq.RESYNC, ("h0",), 1)
