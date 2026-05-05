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
