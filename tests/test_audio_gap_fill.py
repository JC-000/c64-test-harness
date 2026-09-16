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
from c64_test_harness.backends._stream_seq import MAX_HELD_DUPLICATES
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
    MAX_LOST_TIME_FRACTION,
    MEASURED_IDLE_LOSS_MAX,
    MEASURED_LOADED_LOSS_MIN,
    capture_usable,
    fill_near,
    lost_time_fraction,
    payloads_discarded,
)

FILL = None  # marker for an all-zero packet in the decoded order

#: A payload every datagram shares, so a re-sent number is digest-identical
#: to the one already received -- what makes a discard possible at all.
#: Not all-zero, so it is never confused with fill.
SILENT = struct.pack("<H", 0xA5A5) * (AUDIO_PCM_BYTES_PER_PACKET // 2)


def _payload(seq: int, size: int = AUDIO_PCM_BYTES_PER_PACKET) -> bytes:
    return struct.pack("<H", (seq + 1) & 0xFFFF or 1) * (size // 2)


def _capture(
    seqs,
    tmp_path: Path,
    size: int = AUDIO_PCM_BYTES_PER_PACKET,
    payload: bytes | None = None,
):
    cap = AudioCapture(
        port=EPHEMERAL_AUDIO_PORT, bind_addr="127.0.0.1",
        sample_rate=U64_NTSC_AUDIO_RATE_HZ, recv_buf_size=1 << 20,
    )
    cap.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            for i, s in enumerate(seqs):
                body = payload if payload is not None else _payload(s, size)
                tx.sendto(struct.pack("<H", s) + body, ("127.0.0.1", cap.port))
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


def test_a_late_packet_takes_its_own_slot_in_an_asymmetric_burst(tmp_path) -> None:
    """Slot *order*, not just slot count (#449 review).

    Missing 50-54 with seq 51 arriving late: it belongs at offset 1 of the
    burst.  The centre-of-burst case above is symmetric, so it reads the
    same whichever end of ``tracked_missing`` the slots are bound from.
    """
    seqs = list(range(50)) + list(range(55, 60)) + [51]
    result, pcm = _capture(seqs, tmp_path)
    assert (result.packets_dropped, result.packets_filled) == (4, 4)
    assert _order(pcm) == (
        list(range(50)) + [FILL, 51, FILL, FILL, FILL] + list(range(55, 60))
    )
    assert result.filled_frame_ranges == ((50 * 192, 1 * 192), (52 * 192, 3 * 192))
    assert result.time_base_intact is True


def test_a_late_packet_after_an_over_window_gap_keeps_its_absolute_position(
    tmp_path,
) -> None:
    """Untracked fill precedes the tracked slots, so packet 1400 sits at 1400.

    A 1500-packet gap is 477 untracked plus 1023 tracked.  Appending the
    untracked block *after* the tracked slots leaves every count identical
    and moves the late packet 477 positions early (#449 review).
    """
    result, pcm = _capture([0, 1501, 1502, 1503, 1504, 1400], tmp_path)
    assert (result.packets_dropped, result.packets_filled) == (1499, 1499)
    order = _order(pcm)
    assert len(order) == 1505
    assert order.index(1400) == 1400
    assert order[0] == 0
    assert order[1501:] == [1501, 1502, 1503, 1504]
    assert set(order[1:1400]) == {FILL}
    assert set(order[1401:1501]) == {FILL}
    assert result.filled_frame_ranges == (
        (1 * 192, 1399 * 192), (1401 * 192, 100 * 192),
    )
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


#: Every claim the discard clause has to carry, by phrase *and* by figure,
#: in both texts that state it.  Token presence is not a pin: #443, #452,
#: ``packets_reordered`` and "only trace" all live in the restart bullet and
#: the harm paragraph, so asserting those four let the entire held-at-stop
#: bullet be deleted, either figure set be falsified, and the central claim
#: be inverted -- six mutants, none caught (#449 review round 2, E1-E6).
#: The two texts are worded to share these substrings exactly so one table
#: pins both.
_DISCARD_CLAUSE_REQUIRED = (
    # The claim itself.  "fill field" and not "field": once #443 lands
    # payloads_discarded the clause must still be true, and that field is
    # not a fill field.
    "no fill field shows it",
    "#443",
    # Path 1: restart over a number that was itself lost, and its figures.
    "a counter that restarts over a number that was itself lost",
    "52 datagrams in, 41 packets in the WAV, 11 never delivered",
    "#452",
    # Path 2: the tail held at stop().  No #452 dependency -- this is the
    # path that justifies the counter, and the one that can vanish quietly.
    "tail of duplicate-looking datagrams still held",
    "106 in, 100 packets in the WAV, 6 discarded",
    "flush_held",
    "does not depend on #452",
    "MAX_HELD_DUPLICATES",
    # What the harm is, and what is left to see it by.
    "duration, not content",
    "upper bound on packets of lost time",
    "payloads_discarded",
    "packets_reordered",
    "only trace",
    # Why the live gate bounds lost time instead of requiring zero.
    "101 in, 100 in the WAV, 1 discarded",
)

#: Phrasings that would invert the claim while leaving every token above in
#: place (E6).
_DISCARD_CLAUSE_REFUSED = (
    "fill fields show it",
    "the fill fields show",
)


def _clause_texts() -> dict:
    """Both texts with runs of whitespace collapsed to one space.

    Re-wrapping a paragraph is not a change to what it claims, and these
    phrases are longer than a line in the markdown.  Matching the collapsed
    text keeps the pin sensitive to what matters -- a bullet deleted, a
    figure falsified, the claim inverted -- without failing on a reflow.
    """
    md = (Path(__file__).resolve().parent.parent / "docs" / "sid_audio.md").read_text()
    return {
        where: " ".join(text.split())
        for where, text in (
            ("module docstring", uac.__doc__ or ""),
            ("docs/sid_audio.md", md),
        )
    }


@pytest.mark.parametrize("phrase", _DISCARD_CLAUSE_REQUIRED)
def test_both_discard_clause_texts_carry_every_claim(phrase: str) -> None:
    """Each path pinned by phrase and by figure, in both texts (#449 round 2)."""
    for where, text in _clause_texts().items():
        assert phrase in text, f"{where} no longer says {phrase!r}"


@pytest.mark.parametrize("phrase", _DISCARD_CLAUSE_REFUSED)
def test_neither_discard_clause_text_inverts_the_claim(phrase: str) -> None:
    """The fill fields do *not* show a discard; saying they do is the E6 mutant."""
    for where, text in _clause_texts().items():
        assert phrase not in text, f"{where} inverts the claim with {phrase!r}"


def test_the_only_trace_sentence_survives_the_443_merge() -> None:
    """#443's head adds ``payloads_discarded``, so it becomes *the* trace.

    The clause names the counter and scopes the reorder-count claim to a
    result built before it, which is true on both sides of that merge.
    """
    for where, text in _clause_texts().items():
        i = text.index("only trace")
        before = text[:i]
        assert "payloads_discarded" in before, (
            f"{where} claims an only trace without naming payloads_discarded first"
        )
        assert "before that field existed" in before, (
            f"{where} does not scope the only-trace claim to a pre-#443 result"
        )


def _discarding(discarded: int, *, packets_in_wav: int, **kw) -> CaptureResult:
    """A result with *packets_in_wav* packets that discarded *discarded* more.

    ``payloads_discarded`` is set as an attribute so these stand both before
    #443 lands the field and after, when it is a real field on both types.
    """
    r = _r(kw.pop("dropped", 0), kw.pop("filled", 0), packets_in_wav * 192, **kw)
    r.payloads_discarded = discarded
    return r


def test_a_genuine_duplicate_is_discarded_and_the_capture_stays_usable(tmp_path) -> None:
    """The must-pass control: a correct discard must not fail a live run (#443).

    Reviewer-8's row 2 stream, verbatim.  Gating on ``discarded == 0`` fails
    here, which is why the rule bounds lost time instead.
    """
    seqs = list(range(50)) + [48] + list(range(50, 100))
    result, pcm = _capture(seqs, tmp_path)
    assert result.packets_received == 101
    assert len(pcm) // AUDIO_PCM_BYTES_PER_PACKET == 100
    assert (result.packets_dropped, result.packets_filled) == (0, 0)
    assert (result.packets_reordered, result.sequence_resyncs) == (1, 0)
    assert result.time_base_intact is True
    assert _order(pcm) == list(range(100))
    assert capture_usable(result) is True
    # And still usable once #443 lands the counter: 1 of 101 is under bound.
    assert capture_usable(_discarding(1, packets_in_wav=100)) is True
    assert lost_time_fraction(_discarding(1, packets_in_wav=100)) == pytest.approx(1 / 101)


def test_a_restart_over_a_lost_number_reads_clean_in_every_fill_field(tmp_path) -> None:
    """11 datagrams' PCM never delivered, and nothing but the reorder count says so."""
    seqs = [0, 1, 2] + list(range(4, 41)) + list(range(12))
    result, pcm = _capture(seqs, tmp_path, payload=SILENT)
    assert result.packets_received == 52
    assert len(pcm) // AUDIO_PCM_BYTES_PER_PACKET == 41
    assert (result.packets_dropped, result.packets_filled) == (0, 0)
    assert result.fill_fraction == 0.0
    assert result.filled_frame_ranges == ()
    assert result.sequence_resyncs == 0
    assert result.time_base_intact is True
    assert result.packets_reordered > 0, "the only trace there is"
    # 11 of 52 is 21% of the stream: over bound once #443 counts it.
    assert capture_usable(_discarding(11, packets_in_wav=41)) is False
    assert lost_time_fraction(_discarding(11, packets_in_wav=41)) == pytest.approx(11 / 52)


def test_a_held_tail_at_stop_discards_without_loss_or_restart(tmp_path) -> None:
    """The second discard path: no lost packet, no restart, so no #452 dependency."""
    seqs = list(range(100)) + list(range(MAX_HELD_DUPLICATES - 2))
    result, pcm = _capture(seqs, tmp_path, payload=SILENT)
    assert result.packets_received == 106
    assert len(pcm) // AUDIO_PCM_BYTES_PER_PACKET == 100
    assert (result.packets_dropped, result.packets_filled) == (0, 0)
    assert result.sequence_resyncs == 0
    assert result.time_base_intact is True


def test_lost_time_is_bounded_by_share_not_by_count() -> None:
    """The same 6-packet held tail is 5.7% of 106 packets and 0.4% of 1380."""
    assert capture_usable(_discarding(6, packets_in_wav=100)) is False
    real = _discarding(6, packets_in_wav=1380)
    assert lost_time_fraction(real) < MAX_LOST_TIME_FRACTION
    assert capture_usable(real) is True


def test_a_result_without_the_counter_reads_as_none_discarded() -> None:
    """#449 must keep working against a head that has not landed #443 yet."""
    r = _r(0, 0, 100 * 192)
    assert not hasattr(r, "payloads_discarded") or r.payloads_discarded == 0
    assert payloads_discarded(r) == 0
    assert lost_time_fraction(r) == 0.0
    assert capture_usable(r) is True


def test_fill_near_an_edge() -> None:
    r = _r(1, 1, 100 * 192, ranges=((1000, 192),))
    assert fill_near(r, 1000, 0) and fill_near(r, 1191, 0)
    assert not fill_near(r, 1192, 0) and not fill_near(r, 999, 0)
    assert fill_near(r, 990, 10) and fill_near(r, 1201, 10)
    assert not fill_near(r, 1202, 10)
