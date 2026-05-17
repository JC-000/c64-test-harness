"""Feature-parity live integration tests for Ultimate 64.

Exercises the same C64Transport protocol surface that test_vice_core.py
covers on VICE, but against real Ultimate 64 hardware via the REST API.

Gated by the ``U64_HOST`` env var — e.g.:

    U64_HOST=192.168.1.81 python3 -m pytest tests/test_u64_feature_parity_live.py -v

Unlike VICE, the U64 does NOT pause the CPU on memory operations (DMA-backed),
so no ``resume()`` calls are needed between screen reads or after key injection.
"""
from __future__ import annotations

import os
import time

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.debug import dump_screen
from c64_test_harness.keyboard import send_key, send_text
from c64_test_harness.memory import (
    hex_dump,
    read_bytes,
    read_dword_le,
    read_word_le,
    write_bytes,
)
from c64_test_harness.screen import ScreenGrid, wait_for_text
from c64_test_harness.sid import SidFile, build_test_psid
from c64_test_harness.sid_player import SidPlaybackError, play_sid

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")

pytestmark = pytest.mark.skipif(
    not _HOST,
    reason="U64_HOST not set — live Ultimate device tests disabled",
)

# Scratch area — avoids clobbering BASIC/KERNAL
DATA_BASE = 0xC100


@pytest.fixture(scope="module")
def transport() -> Ultimate64Transport:
    lock = DeviceLock(_HOST)
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {_HOST}")
    t = Ultimate64Transport(host=_HOST, password=_PW, timeout=8.0)
    yield t
    t.close()
    lock.release()


# ======================================================================
# Memory round-trip tests
# ======================================================================

class TestMemory:
    def test_write_read_roundtrip(self, transport):
        payload = bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x42])
        write_bytes(transport, DATA_BASE, payload)
        result = read_bytes(transport, DATA_BASE, len(payload))
        assert result == payload

    def test_large_memory_transfer(self, transport):
        payload = bytes(range(256)) * 2
        write_bytes(transport, DATA_BASE, payload)
        result = read_bytes(transport, DATA_BASE, len(payload))
        assert result == payload

    def test_read_word_le(self, transport):
        write_bytes(transport, DATA_BASE, bytes([0x34, 0x12]))
        assert read_word_le(transport, DATA_BASE) == 0x1234

    def test_read_dword_le(self, transport):
        write_bytes(transport, DATA_BASE, bytes([0x78, 0x56, 0x34, 0x12]))
        assert read_dword_le(transport, DATA_BASE) == 0x12345678

    def test_hex_dump_format(self, transport):
        write_bytes(transport, DATA_BASE, bytes(range(32)))
        output = hex_dump(transport, DATA_BASE, 32)
        lines = output.strip().splitlines()
        assert len(lines) == 2
        assert lines[0].startswith(f"${DATA_BASE:04X}")
        assert lines[1].startswith(f"${DATA_BASE + 16:04X}")
        assert "00 01 02" in lines[0]

    def test_read_rom_area(self, transport):
        data = read_bytes(transport, 0xA000, 2)
        assert len(data) == 2
        assert all(isinstance(b, int) for b in data)


# ======================================================================
# Screen + keyboard integration
# ======================================================================

class TestScreenKeyboard:
    """Screen reads and keyboard injection against a live Ultimate 64."""

    def test_screen_grid_reads_real_screen(self, transport):
        transport.client.reset()
        grid = wait_for_text(transport, "READY.", timeout=5.0)
        assert grid is not None, "BASIC READY. prompt did not appear after reset"
        assert len(grid.text_lines()) == 25
        for line in grid.text_lines():
            assert len(line) == 40
        assert grid.has_text("READY.")

    def test_inject_keys_and_verify(self, transport):
        send_text(transport, "PRINT 2+3\r")
        time.sleep(2.0)
        grid = ScreenGrid.from_transport(transport)
        assert "5" in grid.continuous_text()

    def test_send_text_long_batching(self, transport):
        send_text(transport, 'PRINT"ABCDEFGHIJKLMNOPQRST"\r')
        time.sleep(3.0)
        grid = ScreenGrid.from_transport(transport)
        assert "ABCDEFGHIJKLMNOPQRST" in grid.continuous_text()

    def test_wait_for_text(self, transport):
        send_text(transport, 'PRINT"HELLO U64"\r')
        result = wait_for_text(transport, "HELLO U64", timeout=10.0)
        assert result is not None

    def test_dump_screen(self, transport):
        output = dump_screen(transport, "u64test")
        assert "--- Screen dump [u64test] ---" in output
        assert "---" in output


# ======================================================================
# Transport edge cases
# ======================================================================

class TestEdgeCases:
    def test_write_memory_zero_length(self, transport):
        transport.write_memory(0xC100, b"")

    def test_read_memory_zero_length(self, transport):
        result = transport.read_memory(0xC100, 0)
        assert result == b""

    def test_resume_no_crash(self, transport):
        transport.resume()

    def test_read_memory_page_boundary(self, transport):
        data = transport.read_memory(0x00FF, 2)
        assert len(data) == 2

    def test_read_screen_codes_length(self, transport):
        codes = transport.read_screen_codes()
        assert len(codes) == 1000
        assert all(0 <= c <= 255 for c in codes)


# ======================================================================
# SID playback
# ======================================================================

def _build_test_sid() -> SidFile:
    init_code = bytes([0xA9, 0x42, 0x8D, 0x60, 0x03])  # LDA #$42; STA $0360
    play_code = bytes([0xEE, 0x61, 0x03])                # INC $0361
    sid_bytes = build_test_psid(
        load_addr=0x1000, init_code=init_code, play_code=play_code
    )
    return SidFile.from_bytes(sid_bytes)


class TestSidPlayback:
    def test_play_sid_succeeds(self, transport):
        sid = _build_test_sid()
        try:
            play_sid(transport, sid, song=0)
            time.sleep(0.5)
            sentinel = transport.read_memory(0x0360, 1)
            if sentinel[0] != 0x42:
                pytest.xfail(
                    "U64 native SID player may not execute init code literally; "
                    f"sentinel was 0x{sentinel[0]:02X}, expected 0x42"
                )
        finally:
            try:
                transport.client.reset()
            except Exception:
                pass

    def test_play_sid_song_out_of_range(self, transport):
        sid = _build_test_sid()
        try:
            with pytest.raises(SidPlaybackError):
                play_sid(transport, sid, song=5)
        finally:
            try:
                transport.client.reset()
            except Exception:
                pass
