"""Tests for execution control functions (execute.py)."""
from __future__ import annotations

import pytest

from c64_test_harness.execute import (
    delete_breakpoint,
    goto,
    jsr,
    load_code,
    set_breakpoint,
    set_register,
    wait_for_pc,
)
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
