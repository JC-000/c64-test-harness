"""Unit tests for u64_debug_capture module (BusCycle, DebugCapture)."""
from __future__ import annotations

import itertools
import socket
import struct
import time
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.u64_debug_capture import (
    ENTRIES_PER_PACKET,
    ENTRY_SIZE,
    HEADER_SIZE,
    BusCycle,
    DebugCapture,
    DebugCaptureResult,
)


# ---------------------------------------------------------------- helpers


def _build_raw_word(
    *,
    phi2: bool = False,
    rw: bool = True,
    address: int = 0,
    data: int = 0,
    irq: bool = False,
    nmi: bool = False,
    game: bool = False,
    exrom: bool = False,
    rom: bool = False,
    ba: bool = False,
) -> int:
    """Build a 32-bit debug stream word from field values.

    Active-low signals (IRQ#, NMI#, GAME#, EXROM#, ROM#): when the logical
    signal is *asserted* (True), the bit is 0.
    """
    word = address & 0xFFFF
    word |= (data & 0xFF) << 16
    if rw:
        word |= 1 << 24
    if not nmi:   # active low: deasserted = bit high
        word |= 1 << 25
    if not rom:
        word |= 1 << 26
    if not irq:
        word |= 1 << 27
    if ba:
        word |= 1 << 28
    if not exrom:
        word |= 1 << 29
    if not game:
        word |= 1 << 30
    if phi2:
        word |= 1 << 31
    return word


def _build_debug_packet(seq: int, entries: list[int] | None = None) -> bytes:
    """Build a debug capture UDP packet.

    Args:
        seq: 16-bit sequence number.
        entries: list of 32-bit raw words.  Defaults to ENTRIES_PER_PACKET zeros.
    """
    header = struct.pack("<H", seq) + b"\x00\x00"
    if entries is None:
        payload = struct.pack(f"<{ENTRIES_PER_PACKET}I", *([0] * ENTRIES_PER_PACKET))
    else:
        payload = b"".join(struct.pack("<I", w) for w in entries)
    return header + payload


# ---------------------------------------------------------------- BusCycle


class TestBusCycle:
    """Tests for BusCycle bit-field parsing."""

    def test_cpu_read(self):
        raw = _build_raw_word(phi2=True, rw=True, address=0x0400, data=0x42)
        c = BusCycle(raw=raw)
        assert c.is_cpu is True
        assert c.is_vic is False
        assert c.is_read is True
        assert c.is_write is False
        assert c.address == 0x0400
        assert c.data == 0x42

    def test_vic_write(self):
        raw = _build_raw_word(phi2=False, rw=False, address=0xD020, data=0x06)
        c = BusCycle(raw=raw)
        assert c.is_vic is True
        assert c.is_cpu is False
        assert c.is_write is True
        assert c.data == 0x06

    def test_active_low_signals(self):
        raw = _build_raw_word(irq=True, nmi=True, game=True, exrom=True, rom=True)
        c = BusCycle(raw=raw)
        assert c.irq is True
        assert c.nmi is True
        assert c.game is True
        assert c.exrom is True
        assert c.rom is True

    def test_signals_deasserted(self):
        raw = _build_raw_word(irq=False, nmi=False, game=False, exrom=False, rom=False)
        c = BusCycle(raw=raw)
        assert c.irq is False
        assert c.nmi is False
        assert c.game is False
        assert c.exrom is False
        assert c.rom is False

    def test_ba_signal(self):
        raw_high = _build_raw_word(ba=True)
        assert BusCycle(raw=raw_high).ba is True

        raw_low = _build_raw_word(ba=False)
        assert BusCycle(raw=raw_low).ba is False

    def test_address_full_range(self):
        raw = _build_raw_word(address=0xFFFF)
        assert BusCycle(raw=raw).address == 0xFFFF

    def test_data_full_range(self):
        raw = _build_raw_word(data=0xFF)
        assert BusCycle(raw=raw).data == 0xFF

    def test_raw_preserved(self):
        raw = _build_raw_word(phi2=True, rw=False, address=0x1234, data=0xAB)
        c = BusCycle(raw=raw)
        assert c.raw == raw


# ---------------------------------------------------------------- DebugCapture


class TestDebugCapture:
    """Tests for DebugCapture with mocked sockets."""

    def test_start_stop_empty(self):
        mock_sock = MagicMock()
        mock_sock.recvfrom = MagicMock(
            side_effect=itertools.repeat(socket.timeout())
        )

        with patch(
            "c64_test_harness.backends.u64_debug_capture.socket.socket",
            return_value=mock_sock,
        ):
            cap = DebugCapture()
            cap.start()
            time.sleep(0.1)
            result = cap.stop()

        assert result.total_cycles == 0
        assert result.packets_received == 0

    def test_parse_single_packet(self):
        pkt = _build_debug_packet(seq=0)
        mock_sock = MagicMock()
        mock_sock.recvfrom = MagicMock(
            side_effect=itertools.chain(
                [(pkt, ("10.0.0.1", 11002))],
                itertools.repeat(socket.timeout()),
            )
        )

        with patch(
            "c64_test_harness.backends.u64_debug_capture.socket.socket",
            return_value=mock_sock,
        ):
            cap = DebugCapture()
            cap.start()
            time.sleep(0.1)
            result = cap.stop()

        assert result.total_cycles == ENTRIES_PER_PACKET
        assert result.packets_received == 1

    def test_gap_detection(self):
        pkt0 = _build_debug_packet(seq=0)
        pkt5 = _build_debug_packet(seq=5)
        mock_sock = MagicMock()
        mock_sock.recvfrom = MagicMock(
            side_effect=itertools.chain(
                [
                    (pkt0, ("10.0.0.1", 11002)),
                    (pkt5, ("10.0.0.1", 11002)),
                ],
                itertools.repeat(socket.timeout()),
            )
        )

        with patch(
            "c64_test_harness.backends.u64_debug_capture.socket.socket",
            return_value=mock_sock,
        ):
            cap = DebugCapture()
            cap.start()
            time.sleep(0.1)
            result = cap.stop()

        assert result.packets_dropped == 4
        assert result.packets_received == 2

    def test_already_started_raises(self):
        mock_sock = MagicMock()
        mock_sock.recvfrom = MagicMock(
            side_effect=itertools.repeat(socket.timeout())
        )

        with patch(
            "c64_test_harness.backends.u64_debug_capture.socket.socket",
            return_value=mock_sock,
        ):
            cap = DebugCapture()
            cap.start()
            try:
                with pytest.raises(RuntimeError):
                    cap.start()
            finally:
                cap.stop()

    def test_not_started_raises(self):
        cap = DebugCapture()
        with pytest.raises(RuntimeError):
            cap.stop()


# -------------------------------------------------------- bounded + filter


def _run_capture(
    packets: list[bytes],
    *,
    max_bytes: int | None = None,
    filter=None,
    settle: float = 0.15,
):
    """Feed ``packets`` through a mocked socket to a DebugCapture and stop it.

    Returns the (DebugCapture, DebugCaptureResult) tuple so tests can
    inspect both internal buffers and the parsed result.
    """
    mock_sock = MagicMock()
    mock_sock.recvfrom = MagicMock(
        side_effect=itertools.chain(
            [(pkt, ("10.0.0.1", 11002)) for pkt in packets],
            itertools.repeat(socket.timeout()),
        )
    )

    with patch(
        "c64_test_harness.backends.u64_debug_capture.socket.socket",
        return_value=mock_sock,
    ):
        cap = DebugCapture(max_bytes=max_bytes, filter=filter)
        cap.start()
        time.sleep(settle)
        result = cap.stop()
    return cap, result


# Each packet's entry payload is ENTRIES_PER_PACKET * ENTRY_SIZE = 1440 bytes.
_PKT_PAYLOAD_BYTES = ENTRIES_PER_PACKET * ENTRY_SIZE


class TestDebugCaptureMaxBytes:
    """Tests for max_bytes rolling-window eviction."""

    def test_under_limit_keeps_everything(self):
        # 3 packets, each 1440 entry bytes = 4320 total. Limit well above.
        packets = [_build_debug_packet(seq=i) for i in range(3)]
        cap, result = _run_capture(packets, max_bytes=100_000)

        assert result.packets_received == 3
        # All entries retained.
        assert result.total_cycles == 3 * ENTRIES_PER_PACKET
        # Internal tally matches.
        assert cap._raw_bytes_total == 3 * _PKT_PAYLOAD_BYTES

    def test_over_limit_evicts_oldest_fifo(self):
        # Make each packet carry a distinct marker word so we can tell
        # which packet a cycle came from.
        packets = []
        for i in range(5):
            # ENTRIES_PER_PACKET copies of one sentinel word per packet
            word = _build_raw_word(address=0x1000 + i)
            packets.append(
                _build_debug_packet(seq=i, entries=[word] * ENTRIES_PER_PACKET)
            )

        # Cap at 2 payloads worth of bytes: only the two newest chunks
        # should survive after eviction (the while-loop evicts until
        # total <= max_bytes, keeping at least one chunk).
        limit = 2 * _PKT_PAYLOAD_BYTES
        cap, result = _run_capture(packets, max_bytes=limit)

        assert result.packets_received == 5
        # Buffer is bounded to the limit.
        assert cap._raw_bytes_total <= limit
        # And holds exactly 2 chunks (the two newest).
        assert len(cap._raw_chunks) == 2
        # Parsed trace should only contain markers from packets 3 and 4.
        seen_addresses = {c.address for c in result.trace}
        assert seen_addresses == {0x1003, 0x1004}

    def test_zero_limit_keeps_single_chunk(self):
        # max_bytes=0 is a degenerate limit. The eviction loop requires
        # len(_raw_chunks) > 1, so a single chunk is preserved — we don't
        # expect the capture to silently drop every packet.
        packets = [_build_debug_packet(seq=i) for i in range(3)]
        cap, result = _run_capture(packets, max_bytes=0)

        assert result.packets_received == 3
        # Exactly one chunk (the newest) remains.
        assert len(cap._raw_chunks) == 1
        assert result.total_cycles == ENTRIES_PER_PACKET

    def test_default_is_unbounded(self):
        # No max_bytes: all entries retained regardless of volume.
        packets = [_build_debug_packet(seq=i) for i in range(4)]
        cap, result = _run_capture(packets)

        assert result.total_cycles == 4 * ENTRIES_PER_PACKET
        assert cap._raw_bytes_total == 4 * _PKT_PAYLOAD_BYTES


class TestDebugCaptureFilter:
    """Tests for the per-entry filter predicate."""

    def test_filter_keeps_only_matching_words(self):
        # Build a packet with alternating "keep" and "drop" sentinels so
        # we can verify filter selection.
        keep_word = _build_raw_word(phi2=True, address=0xBEEF)
        drop_word = _build_raw_word(phi2=False, address=0xDEAD)
        entries = []
        for _ in range(ENTRIES_PER_PACKET // 2):
            entries.append(keep_word)
            entries.append(drop_word)
        pkt = _build_debug_packet(seq=0, entries=entries)

        # Only keep CPU-phase (phi2=True) cycles.
        cap, result = _run_capture(
            [pkt], filter=lambda w: bool(w & (1 << 31))
        )

        # Half the words survived.
        assert result.total_cycles == ENTRIES_PER_PACKET // 2
        assert all(c.is_cpu for c in result.trace)
        assert all(c.address == 0xBEEF for c in result.trace)

    def test_filter_reject_all_drops_chunk_entirely(self):
        # If the filter rejects every entry, `entry_payload` becomes empty
        # and the `if entry_payload:` guard prevents any chunk append.
        packets = [_build_debug_packet(seq=i) for i in range(2)]
        cap, result = _run_capture(packets, filter=lambda w: False)

        # Packets were still received (gap/seq tracking still runs) …
        assert result.packets_received == 2
        # … but no entries retained.
        assert result.total_cycles == 0
        assert len(cap._raw_chunks) == 0
        assert cap._raw_bytes_total == 0

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    def test_filter_raising_terminates_recv_thread(self):
        # The recv loop does NOT wrap filter calls in try/except; a
        # raising predicate propagates and kills the daemon thread.
        # stop() must still return cleanly via thread.join(timeout=).
        packets = [_build_debug_packet(seq=0)]

        def boom(_word: int) -> bool:
            raise RuntimeError("filter error")

        cap, result = _run_capture(packets, filter=boom)

        # Thread died before capturing anything usable.
        assert result.total_cycles == 0
        # stop() still produced a result dataclass, didn't hang.
        assert isinstance(result, DebugCaptureResult)
        # Thread object should not be alive after stop().
        assert cap._thread is not None
        assert not cap._thread.is_alive()


class TestDebugCaptureFilterAndMaxBytes:
    """Tests for composition of filter + max_bytes."""

    def test_filter_then_bound(self):
        # Build 4 packets; half the words in each are "keep", half "drop".
        # After filtering each payload shrinks to 720 bytes
        # (ENTRIES_PER_PACKET/2 * 4). Limit of 1500 should retain
        # roughly two post-filter chunks.
        keep_word = _build_raw_word(phi2=True, address=0xCAFE)
        drop_word = _build_raw_word(phi2=False, address=0xBAAD)
        entries = []
        for _ in range(ENTRIES_PER_PACKET // 2):
            entries.append(keep_word)
            entries.append(drop_word)

        packets = [_build_debug_packet(seq=i, entries=entries) for i in range(4)]

        post_filter_chunk_bytes = (ENTRIES_PER_PACKET // 2) * ENTRY_SIZE
        limit = 2 * post_filter_chunk_bytes  # exactly two filtered chunks

        cap, result = _run_capture(
            packets,
            max_bytes=limit,
            filter=lambda w: bool(w & (1 << 31)),
        )

        # All received, but kept buffer is bounded AND filtered.
        assert result.packets_received == 4
        assert cap._raw_bytes_total <= limit
        # Every surviving entry matched the predicate.
        assert all(c.is_cpu for c in result.trace)
        assert all(c.address == 0xCAFE for c in result.trace)
        # At most two chunks worth of post-filter entries.
        assert result.total_cycles <= 2 * (ENTRIES_PER_PACKET // 2)
