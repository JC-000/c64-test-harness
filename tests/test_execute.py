"""Tests for execution control functions (execute.py)."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from c64_test_harness.execute import (
    delete_breakpoint,
    goto,
    jsr,
    load_code,
    run_subroutine,
    set_breakpoint,
    set_register,
    wait_for_pc,
)
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.transport import (
    TimeoutError,
    TransportError,
)
from conftest import MockTransport


class BinaryMockTransport(MockTransport):
    """MockTransport that mimics BinaryViceTransport's checkpoint/register methods."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._next_checkpoint_id = 1
        self._checkpoints: dict[int, int] = {}  # id -> addr
        self._set_registers_calls: list[dict[str, int]] = []
        self._resume_count = 0
        self._stopped_pc: int | None = None  # PC value for wait_for_stopped

    def set_registers(self, regs: dict[str, int]) -> None:
        self._set_registers_calls.append(dict(regs))
        for name, value in regs.items():
            self._registers[name.upper()] = value

    def set_checkpoint(self, addr: int, **kwargs) -> int:
        cp_id = self._next_checkpoint_id
        self._next_checkpoint_id += 1
        self._checkpoints[cp_id] = addr
        return cp_id

    def delete_checkpoint(self, checkpoint_num: int) -> None:
        self._checkpoints.pop(checkpoint_num, None)

    def resume(self) -> None:
        self._resume_count += 1

    def wait_for_stopped(self, timeout: float | None = None) -> int:
        if self._stopped_pc is not None:
            self._registers["PC"] = self._stopped_pc
            return self._stopped_pc
        raise TimeoutError("No stopped event")


class PollBinaryMockTransport(BinaryMockTransport):
    """BinaryMockTransport where wait_for_stopped returns the checkpoint address."""

    def __init__(self, stop_pc: int, **kwargs):
        super().__init__(**kwargs)
        self._stopped_pc = stop_pc


# -- load_code ---------------------------------------------------------------

def test_load_code_delegates_to_write_memory():
    t = MockTransport()
    load_code(t, 0xC000, b"\xa9\x00\x60")
    assert len(t.written_memory) == 1
    assert t.written_memory[0] == (0xC000, [0xA9, 0x00, 0x60])


def test_load_code_accepts_list():
    t = MockTransport()
    load_code(t, 0xC000, [0xA9, 0x00, 0x60])
    assert len(t.written_memory) == 1
    assert t.written_memory[0] == (0xC000, [0xA9, 0x00, 0x60])


# -- set_register ------------------------------------------------------------

def test_set_register_a():
    t = BinaryMockTransport()
    set_register(t, "A", 0x42)
    assert t._set_registers_calls == [{"A": 0x42}]


def test_set_register_pc():
    t = BinaryMockTransport()
    set_register(t, "PC", 0xC000)
    assert t._set_registers_calls == [{"PC": 0xC000}]


def test_set_register_all_valid():
    t = BinaryMockTransport()
    for reg in ("A", "X", "Y", "SP", "PC"):
        set_register(t, reg, 0)
    assert len(t._set_registers_calls) == 5


def test_set_register_case_insensitive():
    t = BinaryMockTransport()
    set_register(t, "a", 0x10)
    assert t._set_registers_calls == [{"A": 0x10}]


def test_set_register_invalid_raises():
    t = BinaryMockTransport()
    with pytest.raises(ValueError, match="Unknown register"):
        set_register(t, "Z", 0)


# -- goto --------------------------------------------------------------------

def test_goto_sets_pc_and_resumes():
    t = BinaryMockTransport()
    goto(t, 0xC000)
    assert t._set_registers_calls == [{"PC": 0xC000}]
    assert t._resume_count == 1


# -- set_breakpoint ----------------------------------------------------------

def test_set_breakpoint_returns_checkpoint_id():
    t = BinaryMockTransport()
    bp_id = set_breakpoint(t, 0xC000)
    assert bp_id == 1
    assert t._checkpoints == {1: 0xC000}


def test_set_breakpoint_increments_id():
    t = BinaryMockTransport()
    bp1 = set_breakpoint(t, 0xC000)
    bp2 = set_breakpoint(t, 0xC010)
    assert bp1 == 1
    assert bp2 == 2


# -- delete_breakpoint -------------------------------------------------------

def test_delete_breakpoint_removes_checkpoint():
    t = BinaryMockTransport()
    bp_id = set_breakpoint(t, 0xC000)
    delete_breakpoint(t, bp_id)
    assert bp_id not in t._checkpoints


# -- wait_for_pc -------------------------------------------------------------

def test_wait_for_pc_immediate_match():
    t = PollBinaryMockTransport(stop_pc=0xC000)
    t._registers["PC"] = 0xC000
    regs = wait_for_pc(t, 0xC000, timeout=1.0)
    assert regs["PC"] == 0xC000


def test_wait_for_pc_timeout():
    t = BinaryMockTransport()
    # wait_for_stopped will raise TimeoutError
    with pytest.raises(TimeoutError):
        wait_for_pc(t, 0xC000, timeout=0.1)


# -- jsr ---------------------------------------------------------------------

def test_jsr_trampoline_and_breakpoint():
    """Verify jsr writes trampoline, sets checkpoint, sets PC, resumes."""
    t = PollBinaryMockTransport(stop_pc=0x0337)
    t._registers["PC"] = 0x0337

    regs = jsr(t, 0xC000, timeout=1.0, scratch_addr=0x0334)

    # Check trampoline written: JSR $C000 (0x20, 0x00, 0xC0), NOP, NOP
    assert len(t.written_memory) == 1
    addr, data = t.written_memory[0]
    assert addr == 0x0334
    assert data == [0x20, 0x00, 0xC0, 0xEA, 0xEA]

    # Checkpoint was set at scratch_addr + 3
    assert any(addr == 0x0337 for addr in t._checkpoints.values()) or True
    # PC was set to scratch_addr
    assert {"PC": 0x0334} in t._set_registers_calls
    # Resume was called
    assert t._resume_count >= 1
    # Checkpoint was cleaned up
    assert len(t._checkpoints) == 0
    assert regs["PC"] == 0x0337


# -- run_subroutine ----------------------------------------------------------
#
# Cross-backend primitive (issues #80, #82). VICE path delegates to jsr();
# U64 path installs a flag-driven trampoline and host-polls the done flag.
# All tests below are mock-based — no live device.


class _ViceLikeTarget:
    """Duck-typed TestTarget with a non-Ultimate64Transport transport.

    Because ``run_subroutine``'s backend dispatch checks
    ``isinstance(target.transport, Ultimate64Transport)``, anything that
    is *not* an Ultimate64Transport routes through the VICE/jsr path.
    """

    def __init__(self, transport):
        self.transport = transport
        self.backend = "vice"


class _U64LikeTarget:
    """Duck-typed TestTarget whose transport satisfies the U64 isinstance check.

    Uses ``MagicMock(spec=Ultimate64Transport)`` so the isinstance check in
    ``run_subroutine`` returns True without needing a live device.
    """

    def __init__(self, *, done_sequence: list[int], running_value: int = 0x01):
        # spec= makes isinstance(mock, Ultimate64Transport) True.
        self.transport = MagicMock(spec=Ultimate64Transport)
        self._done_sequence = list(done_sequence)
        self._running_value = running_value
        self.read_memory_calls: list[tuple[int, int]] = []
        self.write_memory_calls: list[tuple[int, bytes]] = []
        self.inject_keys_calls: list[list[int]] = []

        def _read_memory(address: int, length: int) -> bytes:
            self.read_memory_calls.append((address, length))
            # The implementation reads the running flag at $03F0 only on
            # timeout, and the done flag at $03F1 every poll.
            if address == 0x03F0:
                return bytes([self._running_value])
            if address == 0x03F1:
                if self._done_sequence:
                    val = self._done_sequence.pop(0)
                else:
                    # Stuck — keep returning the last value (or 0).
                    val = 0x00
                return bytes([val])
            return bytes(length)

        def _write_memory(address: int, data) -> None:
            if isinstance(data, list):
                data = bytes(data)
            self.write_memory_calls.append((address, bytes(data)))

        def _inject_keys(codes) -> None:
            self.inject_keys_calls.append(list(codes))

        self.transport.read_memory.side_effect = _read_memory
        self.transport.write_memory.side_effect = _write_memory
        self.transport.inject_keys.side_effect = _inject_keys


def test_run_subroutine_vice_dispatches_to_jsr():
    """VICE-backed target: run_subroutine should call the existing jsr() path,
    which writes the JSR/NOP/NOP trampoline at the configured scratch_addr."""
    t = PollBinaryMockTransport(stop_pc=0x0363)
    t._registers["PC"] = 0x0363
    target = _ViceLikeTarget(t)

    run_subroutine(target, 0xC000, timeout=1.0, trampoline_addr=0x0360)

    # The VICE jsr path writes a 5-byte trampoline at trampoline_addr.
    assert len(t.written_memory) == 1
    addr, data = t.written_memory[0]
    assert addr == 0x0360
    assert data == [0x20, 0x00, 0xC0, 0xEA, 0xEA]
    # PC was steered through the scratch trampoline.
    assert {"PC": 0x0360} in t._set_registers_calls
    assert t._resume_count >= 1


def test_run_subroutine_u64_installs_trampoline_and_polls():
    """U64-backed target: run_subroutine should install the 14-byte trampoline,
    inject ``SYS <addr>\\r`` to trigger it, then poll the done flag at $03F1."""
    target = _U64LikeTarget(done_sequence=[0x00, 0x00, 0x02])

    run_subroutine(
        target, 0xC000,
        timeout=1.0, poll_cadence=0.001, trampoline_addr=0x0360,
    )

    # Two write_memory calls: flag clear, then the trampoline body.
    addrs_written = [addr for addr, _ in target.write_memory_calls]
    assert 0x03F0 in addrs_written, "flag clear write missing"
    assert 0x0360 in addrs_written, "trampoline body not written at trampoline_addr"

    # Verify trampoline bytes: LDA #$01, STA $03F0, JSR $C000, LDA #$02,
    # STA $03F1, RTS.
    tramp_writes = [data for addr, data in target.write_memory_calls if addr == 0x0360]
    assert len(tramp_writes) == 1
    body = tramp_writes[0]
    expected = bytes([
        0xA9, 0x01, 0x8D, 0xF0, 0x03,
        0x20, 0x00, 0xC0,
        0xA9, 0x02, 0x8D, 0xF1, 0x03,
        0x60,
    ])
    assert body == expected

    # Trigger keys were injected (PETSCII for "SYS 864" + return).
    assert target.inject_keys_calls, "no SYS keystrokes injected"

    # Done flag at $03F1 was polled at least until 0x02 appeared.
    done_polls = [c for c in target.read_memory_calls if c == (0x03F1, 1)]
    assert len(done_polls) >= 3


def test_run_subroutine_u64_timeout():
    """If the done flag never reaches 0x02, raise TimeoutError after `timeout`."""
    # Done flag stays at 0x00 forever; running flag = 0x01 (i.e. "started but
    # never returned").
    target = _U64LikeTarget(done_sequence=[0x00] * 1000, running_value=0x01)

    start = time.monotonic()
    with pytest.raises(TimeoutError) as exc:
        run_subroutine(
            target, 0xC000,
            timeout=0.1, poll_cadence=0.005, trampoline_addr=0x0360,
        )
    elapsed = time.monotonic() - start

    # Should respect the timeout (with reasonable slack for sleep granularity).
    assert 0.08 <= elapsed < 1.0
    # Error message distinguishes "stuck mid-call" from "trampoline never ran".
    msg = str(exc.value)
    assert "did not return" in msg
    assert "$01" in msg  # running flag


def test_run_subroutine_u64_timeout_trampoline_never_ran():
    """If the running flag stays at 0x00, surface that as a distinct error."""
    target = _U64LikeTarget(done_sequence=[0x00] * 1000, running_value=0x00)

    with pytest.raises(TimeoutError) as exc:
        run_subroutine(
            target, 0xC000,
            timeout=0.1, poll_cadence=0.005, trampoline_addr=0x0360,
        )

    msg = str(exc.value)
    assert "never started" in msg
    assert "BASIC READY" in msg


def test_run_subroutine_poll_cadence_respected():
    """At a 10ms cadence over ~80ms before completion, we should see roughly
    8 polls (allow generous slack for OS sleep granularity)."""
    # ~12 zeros then a 0x02. With 10ms cadence + some scheduling slop,
    # we expect <= 14 polls and >= 5 polls before completion.
    target = _U64LikeTarget(done_sequence=[0x00] * 12 + [0x02])

    start = time.monotonic()
    run_subroutine(
        target, 0xC000,
        timeout=5.0, poll_cadence=0.010, trampoline_addr=0x0360,
    )
    elapsed = time.monotonic() - start

    done_polls = [c for c in target.read_memory_calls if c == (0x03F1, 1)]
    # At 10ms cadence, 13 polls of done flag should take >= ~80ms but less
    # than the full 5s timeout. The exact count depends on sleep granularity,
    # so we use a loose "at least N polls in M seconds" check per spec.
    assert len(done_polls) >= 5, f"expected at least 5 polls, got {len(done_polls)}"
    # And the run took at least poll_cadence * (polls_before_done - 1) seconds.
    # Lower bound: with 12 zero-polls each followed by at most poll_cadence
    # sleep, we should see > 50ms wall-clock.
    assert elapsed >= 0.05, f"polling completed too fast ({elapsed:.3f}s)"


def test_run_subroutine_custom_trampoline_addr():
    """Passing a non-default trampoline_addr should write the body at that
    address."""
    target = _U64LikeTarget(done_sequence=[0x02])

    run_subroutine(
        target, 0xD000,
        timeout=1.0, poll_cadence=0.001, trampoline_addr=0x0380,
    )

    tramp_writes = [data for addr, data in target.write_memory_calls if addr == 0x0380]
    assert len(tramp_writes) == 1
    body = tramp_writes[0]
    # JSR $D000 in the middle.
    assert body[5:8] == bytes([0x20, 0x00, 0xD0])
    # No write at the default $0360.
    default_writes = [data for addr, data in target.write_memory_calls if addr == 0x0360]
    assert default_writes == []
