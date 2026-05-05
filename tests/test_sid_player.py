"""Unit tests for the sid_player dispatcher and VICE stub builder.

All VICE/U64 transport interactions are mocked — no emulator or hardware
is touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.sid import SidFile
from c64_test_harness.sid_player import (
    DEFAULT_STUB_ADDR,
    SidPlaybackError,
    build_vice_stub,
    play_sid,
    play_sid_ultimate64,
    play_sid_vice,
    stop_sid_vice,
)


def make_sid(
    *,
    load_addr: int = 0x1000,
    init_addr: int = 0x1000,
    play_addr: int = 0x1003,
    songs: int = 3,
    start_song: int = 1,
    data: bytes = b"\x60" * 16,  # RTS payload
) -> SidFile:
    """Build a minimal SidFile object for testing.

    Constructs raw bytes so that ``c64_data`` and ``effective_load_addr``
    work correctly: data_offset is 0x7C (v2), load_addr is embedded only
    when header load_addr == 0.
    """
    data_offset = 0x7C
    # Header (dummy) + data area.
    header = b"\x00" * data_offset
    if load_addr == 0:
        # Embed load address as little-endian prefix.
        body = bytes([0x00, 0x20]) + data  # arbitrary prefix
    else:
        body = data
    raw = header + body
    return SidFile(
        raw=raw,
        format="PSID",
        version=2,
        data_offset=data_offset,
        load_addr=load_addr,
        init_addr=init_addr,
        play_addr=play_addr,
        songs=songs,
        start_song=start_song,
        speed=0,
        name="test",
        author="test",
        released="2026",
        flags=0,
        start_page=0,
        page_length=0,
        second_sid_address=0,
        third_sid_address=0,
    )


# ---------- build_vice_stub ----------

def test_build_vice_stub_length():
    stub = build_vice_stub(0x1234)
    assert len(stub) == 18


def test_build_vice_stub_wrapper_contents():
    # Default stub_addr = 0xC000 → wrapper at 0xC00C.
    stub = build_vice_stub(0x1234)
    # Wrapper lives at offset 12.
    wrapper = stub[12:]
    assert wrapper == bytes([0x20, 0x34, 0x12, 0x4C, 0x31, 0xEA])


def test_build_vice_stub_installer_stores_at_irq_vector():
    stub = build_vice_stub(0x1234)
    # Installer layout (high-byte-first vector update):
    #   0:    A9 hi         LDA #>wrapper
    #   1:    (immediate hi)
    #   2,3,4: 8D 15 03     STA $0315
    #   5:    A9 lo         LDA #<wrapper
    #   6:    (immediate lo)
    #   7,8,9: 8D 14 03     STA $0314
    #  10:    EA            NOP
    #  11:    60            RTS
    assert stub[0] == 0xA9  # LDA #
    assert stub[2] == 0x8D and stub[3] == 0x15 and stub[4] == 0x03  # STA $0315
    assert stub[5] == 0xA9  # LDA #
    assert stub[7] == 0x8D and stub[8] == 0x14 and stub[9] == 0x03  # STA $0314


def test_build_vice_stub_installer_encodes_wrapper_address():
    """LDA #<wrapper / LDA #>wrapper must reference stub_addr + 12."""
    stub_addr = 0xC000
    stub = build_vice_stub(0x1234, stub_addr=stub_addr)
    wrapper_addr = stub_addr + 12  # 0xC00C
    # hi-byte immediate at offset 1, lo-byte immediate at offset 6.
    assert stub[1] == ((wrapper_addr >> 8) & 0xFF)  # 0xC0
    assert stub[6] == (wrapper_addr & 0xFF)         # 0x0C


def test_build_vice_stub_installer_wrapper_nondefault_base():
    stub_addr = 0x2000
    stub = build_vice_stub(0xABCD, stub_addr=stub_addr)
    # wrapper_addr = 0x200C
    assert stub[1] == 0x20  # hi
    assert stub[6] == 0x0C  # lo
    # Wrapper still encodes play_addr correctly.
    assert stub[12:] == bytes([0x20, 0xCD, 0xAB, 0x4C, 0x31, 0xEA])


def test_build_vice_stub_tail_is_rts():
    stub = build_vice_stub(0x1234)
    assert stub[10] == 0xEA  # NOP padding
    assert stub[11] == 0x60  # RTS


# ---------- _validate_song via play_sid_vice ----------

def test_play_sid_vice_song_out_of_range_high():
    sid = make_sid(songs=3)
    t = MagicMock()
    with pytest.raises(SidPlaybackError, match="out of range"):
        play_sid_vice(t, sid, song=3)


def test_play_sid_vice_song_out_of_range_negative():
    sid = make_sid(songs=3)
    t = MagicMock()
    with pytest.raises(SidPlaybackError, match="out of range"):
        play_sid_vice(t, sid, song=-1)


def test_play_sid_vice_play_addr_zero_raises():
    sid = make_sid(play_addr=0)
    t = MagicMock()
    with pytest.raises(SidPlaybackError, match="play_addr is 0"):
        play_sid_vice(t, sid, song=0)


# ---------- play_sid_vice happy path ----------

def test_play_sid_vice_writes_data_and_stub_and_resumes(monkeypatch):
    sid = make_sid(
        load_addr=0x2000,
        init_addr=0x2000,
        play_addr=0x2003,
        songs=2,
    )
    t = MagicMock()

    jsr_calls = []

    def fake_jsr(transport, addr, **kwargs):
        jsr_calls.append(addr)
        return {}

    monkeypatch.setattr("c64_test_harness.execute.jsr", fake_jsr)

    play_sid_vice(t, sid, song=1, stub_addr=0xC000)

    # write_memory called for: SID data, stub, song trampoline.
    write_calls = t.write_memory.call_args_list
    # Extract (addr, data) pairs.
    addrs = [c.args[0] for c in write_calls]
    assert sid.effective_load_addr in addrs  # 0x2000
    assert 0xC000 in addrs                    # stub
    assert 0x033C in addrs                    # song trampoline

    # The song trampoline write should contain LDA #1.
    trampoline_write = next(c for c in write_calls if c.args[0] == 0x033C)
    tramp = trampoline_write.args[1]
    assert tramp[0] == 0xA9  # LDA #
    assert tramp[1] == 1     # song 1
    assert tramp[2] == 0x20  # JSR
    assert tramp[3] == 0x00  # init_addr lo
    assert tramp[4] == 0x20  # init_addr hi

    # jsr() called twice: once for song trampoline, once for installer.
    assert jsr_calls == [0x033C, 0xC000]

    # resume() called at the end.
    t.resume.assert_called_once()


def test_play_sid_vice_song_zero_trampoline(monkeypatch):
    sid = make_sid(init_addr=0x1000)
    t = MagicMock()
    monkeypatch.setattr("c64_test_harness.execute.jsr", lambda *a, **k: {})

    play_sid_vice(t, sid, song=0)

    trampoline_write = next(
        c for c in t.write_memory.call_args_list if c.args[0] == 0x033C
    )
    assert trampoline_write.args[1][1] == 0  # song = 0


# ---------- play_sid_ultimate64 ----------

def test_play_sid_ultimate64_calls_client_sid_play():
    sid = make_sid(songs=5)
    t = MagicMock()
    play_sid_ultimate64(t, sid, song=2)
    t._client.sid_play.assert_called_once_with(sid.raw, songnr=2)


def test_play_sid_ultimate64_song_zero():
    sid = make_sid(songs=5)
    t = MagicMock()
    play_sid_ultimate64(t, sid, song=0)
    t._client.sid_play.assert_called_once_with(sid.raw, songnr=0)


def test_play_sid_ultimate64_song_out_of_range():
    sid = make_sid(songs=3)
    t = MagicMock()
    with pytest.raises(SidPlaybackError, match="out of range"):
        play_sid_ultimate64(t, sid, song=5)


# ---------- dispatcher ----------

def test_play_sid_dispatches_to_u64():
    from c64_test_harness.backends.ultimate64 import Ultimate64Transport

    t = MagicMock(spec=Ultimate64Transport)
    t._client = MagicMock()
    sid = make_sid(songs=2)
    play_sid(t, sid, song=1)
    t._client.sid_play.assert_called_once_with(sid.raw, songnr=1)


def test_play_sid_dispatches_to_vice(monkeypatch):
    from c64_test_harness.backends.vice_binary import BinaryViceTransport

    t = MagicMock(spec=BinaryViceTransport)
    sid = make_sid(songs=2)
    monkeypatch.setattr("c64_test_harness.execute.jsr", lambda *a, **k: {})
    play_sid(t, sid, song=0)
    # VICE path calls write_memory multiple times.
    assert t.write_memory.call_count >= 3
    t.resume.assert_called_once()


def test_play_sid_unsupported_transport():
    class Foo:
        pass
    with pytest.raises(SidPlaybackError, match="Unsupported transport"):
        play_sid(Foo(), make_sid())


# ---------- stop_sid_vice ----------

def test_stop_sid_vice_restores_kernal_irq():
    t = MagicMock()
    stop_sid_vice(t)
    t.write_memory.assert_called_once_with(0x0314, bytes([0x31, 0xEA]))
