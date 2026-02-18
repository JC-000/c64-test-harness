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
from c64_test_harness.transport import TimeoutError, TransportError
from conftest import MockTransport


class ExecuteMockTransport(MockTransport):
    """MockTransport with configurable raw_command responses."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._raw_responses: list[str] = []
        self._raw_response_idx = 0

    def set_raw_responses(self, responses: list[str]) -> None:
        self._raw_responses = responses
        self._raw_response_idx = 0

    def raw_command(self, cmd: str) -> str:
        self._raw_commands.append(cmd)
        if self._raw_response_idx < len(self._raw_responses):
            resp = self._raw_responses[self._raw_response_idx]
            self._raw_response_idx += 1
            return resp
        return ""


class PollMockTransport(ExecuteMockTransport):
    """MockTransport where read_registers returns different PC values on successive calls."""

    def __init__(self, pc_sequence: list[int], **kwargs):
        super().__init__(**kwargs)
        self._pc_sequence = pc_sequence
        self._pc_idx = 0

    def read_registers(self) -> dict[str, int]:
        regs = dict(self._registers)
        if self._pc_idx < len(self._pc_sequence):
            regs["PC"] = self._pc_sequence[self._pc_idx]
            self._pc_idx += 1
        return regs


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
    t = MockTransport()
    set_register(t, "A", 0x42)
    assert t._raw_commands == ["r A = $42"]


def test_set_register_pc():
    t = MockTransport()
    set_register(t, "PC", 0xC000)
    assert t._raw_commands == ["r PC = $c000"]


def test_set_register_all_valid():
    t = MockTransport()
    for reg in ("A", "X", "Y", "SP", "PC"):
        set_register(t, reg, 0)
    assert len(t._raw_commands) == 5


def test_set_register_case_insensitive():
    t = MockTransport()
    set_register(t, "a", 0x10)
    assert t._raw_commands == ["r A = $10"]


def test_set_register_invalid_raises():
    t = MockTransport()
    with pytest.raises(ValueError, match="Unknown register"):
        set_register(t, "Z", 0)


# -- goto --------------------------------------------------------------------

def test_goto_sets_pc():
    t = MockTransport()
    goto(t, 0xC000)
    assert t._raw_commands == ["r PC = $c000"]


# -- set_breakpoint ----------------------------------------------------------

def test_set_breakpoint_parses_id():
    t = ExecuteMockTransport()
    t.set_raw_responses(["BREAK: 1  C:$c000"])
    bp_id = set_breakpoint(t, 0xC000)
    assert bp_id == 1
    assert t._raw_commands == ["break $c000"]


def test_set_breakpoint_parse_failure():
    t = ExecuteMockTransport()
    t.set_raw_responses(["garbage response"])
    with pytest.raises(TransportError, match="Failed to parse breakpoint"):
        set_breakpoint(t, 0xC000)


# -- delete_breakpoint -------------------------------------------------------

def test_delete_breakpoint_sends_command():
    t = MockTransport()
    delete_breakpoint(t, 3)
    assert t._raw_commands == ["delete 3"]


# -- wait_for_pc -------------------------------------------------------------

def test_wait_for_pc_immediate_match():
    t = PollMockTransport(pc_sequence=[0xC000])
    regs = wait_for_pc(t, 0xC000, timeout=1.0, poll_interval=0.01)
    assert regs["PC"] == 0xC000


def test_wait_for_pc_reaches_after_polls():
    t = PollMockTransport(pc_sequence=[0x0800, 0x0900, 0xC000])
    regs = wait_for_pc(t, 0xC000, timeout=2.0, poll_interval=0.01)
    assert regs["PC"] == 0xC000


def test_wait_for_pc_timeout():
    t = PollMockTransport(pc_sequence=[0x0800] * 100)
    with pytest.raises(TimeoutError, match="did not reach"):
        wait_for_pc(t, 0xC000, timeout=0.1, poll_interval=0.01)


# -- jsr ---------------------------------------------------------------------

def test_jsr_trampoline_and_breakpoint():
    """Verify jsr writes trampoline, sets breakpoint, and calls goto."""
    t = PollMockTransport(pc_sequence=[0x0337])  # scratch_addr + 3
    t.set_raw_responses(["BREAK: 5  C:$0337"])  # set_breakpoint response

    regs = jsr(t, 0xC000, timeout=1.0, scratch_addr=0x0334)

    # Check trampoline written: JSR $C000 (0x20, 0x00, 0xC0), NOP, NOP
    assert len(t.written_memory) == 1
    addr, data = t.written_memory[0]
    assert addr == 0x0334
    assert data == [0x20, 0x00, 0xC0, 0xEA, 0xEA]

    # Breakpoint was set at scratch_addr + 3
    assert "break $0337" in t._raw_commands

    # goto was called (sets PC to scratch_addr)
    assert "r PC = $0334" in t._raw_commands

    # Breakpoint was cleaned up
    assert "delete 5" in t._raw_commands

    assert regs["PC"] == 0x0337
