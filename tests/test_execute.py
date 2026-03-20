"""Tests for execution control functions (execute.py)."""
from __future__ import annotations

import pytest

from c64_test_harness.execute import (
    delete_breakpoint,
    goto,
    jsr,
    jsr_poll,
    load_code,
    set_breakpoint,
    set_register,
    wait_for_pc,
)
from c64_test_harness.transport import (
    TimeoutError,
    TransportError,
    ConnectionError as TransportConnectionError,
)
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


def test_jsr_custom_poll_interval():
    """Verify jsr passes poll_interval through to wait_for_pc."""
    t = PollMockTransport(pc_sequence=[0x0337])
    t.set_raw_responses(["BREAK: 5  C:$0337"])

    regs = jsr(t, 0xC000, timeout=1.0, scratch_addr=0x0334, poll_interval=1.5)

    assert regs["PC"] == 0x0337
    # Trampoline still written correctly
    addr, data = t.written_memory[0]
    assert addr == 0x0334
    assert data == [0x20, 0x00, 0xC0, 0xEA, 0xEA]


# -- jsr_poll ----------------------------------------------------------------

class JsrPollMockTransport(MockTransport):
    """MockTransport with configurable read_memory responses for flag polling."""

    def __init__(self, flag_sequence: list[int | Exception], **kwargs):
        super().__init__(**kwargs)
        self._flag_sequence = flag_sequence
        self._flag_idx = 0
        self._read_memory_calls: list[tuple[int, int]] = []
        self._registers_error: Exception | None = None

    def read_memory(self, addr: int, length: int) -> bytes:
        self._read_memory_calls.append((addr, length))
        if self._flag_idx < len(self._flag_sequence):
            val = self._flag_sequence[self._flag_idx]
            self._flag_idx += 1
            if isinstance(val, Exception):
                raise val
            return bytes([val] * length)
        return bytes(length)

    def read_registers(self) -> dict[str, int]:
        if self._registers_error is not None:
            err = self._registers_error
            self._registers_error = None
            raise err
        return dict(self._registers)


def test_jsr_poll_basic_success():
    """Flag returns $FF on first poll — verify trampoline, flag clear, PC set, registers returned."""
    t = JsrPollMockTransport(flag_sequence=[0xFF])

    regs = jsr_poll(t, 0xC000, timeout=2.0, scratch_addr=0x0334, poll_interval=0.01)

    # Trampoline written: 17 bytes at scratch_addr
    # Layout: LDA #$00, STA flag, JSR addr, LDA #$FF, STA flag, JMP loop, flag
    assert len(t.written_memory) >= 1
    addr, data = t.written_memory[0]
    assert addr == 0x0334
    assert len(data) == 17
    # LDA #$00 (clear flag)
    assert data[0:2] == [0xA9, 0x00]
    # STA flag_addr (0x0334 + 16 = 0x0344)
    assert data[2:5] == [0x8D, 0x44, 0x03]
    # JSR $C000
    assert data[5:8] == [0x20, 0x00, 0xC0]
    # LDA #$FF (set flag)
    assert data[8:10] == [0xA9, 0xFF]
    # STA flag_addr
    assert data[10:13] == [0x8D, 0x44, 0x03]
    # JMP loop (loop_addr = 0x0334 + 15 = 0x0343)
    assert data[13:16] == [0x4C, 0x43, 0x03]
    # Flag byte at end
    assert data[16] == 0x00

    # Flag cleared to $00
    flag_write = t.written_memory[1]
    assert flag_write == (0x0344, [0x00])

    # PC set to scratch_addr
    assert "r PC = $0334" in t._raw_commands

    # Registers returned
    assert "PC" in regs


def test_jsr_poll_polls_until_flag_set():
    """read_memory returns $00 twice then $FF — verify multiple polls."""
    t = JsrPollMockTransport(flag_sequence=[0x00, 0x00, 0xFF])

    regs = jsr_poll(t, 0xC000, timeout=2.0, scratch_addr=0x0334, poll_interval=0.01)

    # Should have polled 3 times (flag_addr reads)
    flag_reads = [(a, l) for a, l in t._read_memory_calls if a == 0x0344]
    assert len(flag_reads) == 3
    assert "PC" in regs


def test_jsr_poll_timeout():
    """read_memory always returns $00 — verify TimeoutError."""
    t = JsrPollMockTransport(flag_sequence=[0x00] * 200)

    with pytest.raises(TimeoutError, match="did not return"):
        jsr_poll(t, 0xC000, timeout=0.05, scratch_addr=0x0334, poll_interval=0.01)


def test_jsr_poll_connection_error_retried():
    """TransportConnectionError during poll is silently retried."""
    t = JsrPollMockTransport(flag_sequence=[
        TransportConnectionError("connection lost"),
        0xFF,
    ])

    regs = jsr_poll(t, 0xC000, timeout=2.0, scratch_addr=0x0334, poll_interval=0.01)

    # Should succeed after retry
    assert "PC" in regs
    # Two read_memory calls total (one error, one success)
    flag_reads = [(a, l) for a, l in t._read_memory_calls if a == 0x0344]
    assert len(flag_reads) == 2


def test_jsr_poll_connection_error_on_read_registers():
    """Flag detected as $FF but read_registers raises — returns empty dict."""
    t = JsrPollMockTransport(flag_sequence=[0xFF])
    t._registers_error = TransportConnectionError("monitor gone")

    regs = jsr_poll(t, 0xC000, timeout=2.0, scratch_addr=0x0334, poll_interval=0.01)

    assert regs == {}


def test_jsr_poll_custom_scratch_addr():
    """Verify trampoline uses correct addresses with custom scratch_addr."""
    custom_addr = 0xC000
    flag_addr = custom_addr + 16  # 0xC010
    loop_addr = custom_addr + 15  # 0xC00F

    t = JsrPollMockTransport(flag_sequence=[0xFF])

    regs = jsr_poll(t, 0x1234, timeout=2.0, scratch_addr=custom_addr, poll_interval=0.01)

    # Trampoline at custom address
    addr, data = t.written_memory[0]
    assert addr == custom_addr
    assert len(data) == 17
    # LDA #$00
    assert data[0:2] == [0xA9, 0x00]
    # STA flag (0xC010)
    assert data[2:5] == [0x8D, 0x10, 0xC0]
    # JSR $1234
    assert data[5:8] == [0x20, 0x34, 0x12]
    # LDA #$FF
    assert data[8:10] == [0xA9, 0xFF]
    # STA flag (0xC010)
    assert data[10:13] == [0x8D, 0x10, 0xC0]
    # JMP loop (0xC00F)
    assert data[13:16] == [0x4C, 0x0F, 0xC0]

    # Flag cleared at correct address
    flag_write = t.written_memory[1]
    assert flag_write == (flag_addr, [0x00])

    # PC set to custom scratch_addr
    assert f"r PC = ${custom_addr:04x}" in t._raw_commands

    assert "PC" in regs
