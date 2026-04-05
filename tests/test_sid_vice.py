"""Live integration tests for SID playback on VICE.

Launches a VICE instance via ``ViceInstanceManager``, synthesizes a test
PSID whose init/play routines have observable RAM side effects, plays
it, and verifies the memory sentinels to confirm playback is running.

Requires ``x64sc`` on PATH.
"""

from __future__ import annotations

import shutil
import struct
import time

import pytest

from c64_test_harness.backends.vice_manager import ViceInstanceManager
from c64_test_harness.backends.vice_lifecycle import ViceConfig
from c64_test_harness.screen import wait_for_text
from c64_test_harness.sid import SidFile, build_test_psid
from c64_test_harness.sid_player import (
    SidPlaybackError,
    play_sid,
    play_sid_vice,
    stop_sid_vice,
)


pytestmark = pytest.mark.skipif(
    shutil.which("x64sc") is None, reason="x64sc not found on PATH"
)


def _build_test_sid() -> SidFile:
    """Synthesize a PSID whose init writes a sentinel and play ticks a counter.

    Layout at $1000:
        LDA #$42 ; STA $0360 ; RTS   (init, 6 bytes)
        INC $0361 ; RTS               (play, 4 bytes)

    The sentinel region is chosen to avoid the cassette buffer slot
    ($033C-$0341) where ``sid_player`` writes its song trampoline.
    """
    init_code = bytes([0xA9, 0x42, 0x8D, 0x60, 0x03])
    play_code = bytes([0xEE, 0x61, 0x03])
    sid_bytes = build_test_psid(
        load_addr=0x1000, init_code=init_code, play_code=play_code
    )
    sid = SidFile.from_bytes(sid_bytes)
    assert sid.init_addr == 0x1000
    assert sid.play_addr == 0x1006
    return sid


def _make_irq_sid() -> SidFile:
    """Build a minimal SidFile with play_addr == 0 (IRQ-driven SID)."""
    init_code = bytes([0xA9, 0x42])
    sid_bytes = bytearray(
        build_test_psid(load_addr=0x1000, init_code=init_code, play_code=b"")
    )
    # Overwrite play_addr (header offset 12..13, big-endian) with 0.
    struct.pack_into(">H", sid_bytes, 12, 0)
    return SidFile.from_bytes(bytes(sid_bytes))


@pytest.fixture(scope="module")
def vice_mgr():
    config = ViceConfig(warp=True, sound=False)
    with ViceInstanceManager(config=config) as mgr:
        yield mgr


def test_play_sid_on_vice(vice_mgr) -> None:
    """Play the test SID and verify init ran + IRQ is ticking play."""
    sid = _build_test_sid()
    with vice_mgr.instance() as vm:
        transport = vm.transport
        # Wait for BASIC READY prompt so we know KERNAL IRQ is active.
        wait_for_text(transport, "READY.", timeout=30.0)

        # Zero the sentinels first.
        transport.write_memory(0x0360, bytes([0x00, 0x00]))

        # Play the SID.
        play_sid(transport, sid, song=0)

        # Give IRQ several jiffies to fire (warp mode, so wall-clock 0.5s
        # yields many more than 25 play calls — but $0361 wraps at 256).
        time.sleep(0.5)

        mem = transport.read_memory(0x0360, 2)
        assert mem[0] == 0x42, (
            f"init didn't write sentinel; got ${mem[0]:02X}"
        )
        assert mem[1] > 5, (
            f"play didn't run enough (counter=${mem[1]:02X})"
        )

        # Stop playback so later tests aren't disturbed.
        stop_sid_vice(transport)


def test_play_sid_vice_song_out_of_range(vice_mgr) -> None:
    """Song index >= songs must raise SidPlaybackError."""
    sid = _build_test_sid()
    # Test SID has songs=1, so song=5 is out of range.
    with vice_mgr.instance() as vm:
        transport = vm.transport
        wait_for_text(transport, "READY.", timeout=30.0)
        with pytest.raises(SidPlaybackError, match="out of range"):
            play_sid_vice(transport, sid, song=5)


def test_play_sid_vice_irq_driven_sid_raises(vice_mgr) -> None:
    """A SID with play_addr==0 must raise SidPlaybackError on VICE."""
    sid = _make_irq_sid()
    assert sid.play_addr == 0
    with vice_mgr.instance() as vm:
        transport = vm.transport
        wait_for_text(transport, "READY.", timeout=30.0)
        with pytest.raises(SidPlaybackError, match="IRQ-driven"):
            play_sid_vice(transport, sid, song=0)


def test_stop_sid_vice_restores_kernal_irq_vector(vice_mgr) -> None:
    """After stop_sid_vice, $0314/$0315 must point back at $EA31."""
    sid = _build_test_sid()
    with vice_mgr.instance() as vm:
        transport = vm.transport
        wait_for_text(transport, "READY.", timeout=30.0)
        play_sid(transport, sid, song=0)
        time.sleep(0.2)
        stop_sid_vice(transport)
        # Allow any in-flight IRQ to finish.
        time.sleep(0.05)
        vec = transport.read_memory(0x0314, 2)
        assert vec[0] == 0x31 and vec[1] == 0xEA, (
            f"expected $0314/$0315 = $31 $EA, got ${vec[0]:02X} ${vec[1]:02X}"
        )
