"""The UCI status string must stay inside its 240-byte buffer (issue #281).

Layout (``uci_network.py``): the status buffer starts at ``$C300`` and the
next field is the response length at ``$C3F0``, so the buffer holds
``$C3F0 - $C300 = 240`` bytes.  Behind it sit ``$C3F0-$C3F1`` (response
length), ``$C3F2-$C3F3`` (status length), ``$C3FE`` (sentinel) and
``$C3FF`` (error flag).

The firmware bound is **256**, not 240: ``CMD_MAX_STATUS_LEN 256``
(``software/io/command_interface/command_intf.h:58``).  So a status longer
than the buffer is something the firmware is allowed to send; nothing
restricts it to the three short ``GET_IPADDR`` strings.

Two defects, one on each side of the wire:

* **The 6502 drain** (``_build_read_status``) stored every byte at
  ``$C300,Y`` with ``INY / BNE loop``, so a 241..256-byte status wrote
  through ``$C3F0``-``$C3FF`` -- response length, status length, sentinel
  and error flag -- and a 256-byte one wrapped Y and recorded length 0.
* **The host read** (``_read_status_string``) read one byte of the
  documented 2-byte little-endian length and then read that many bytes
  from ``$C300``, which for a length above 240 runs past the buffer.

The drain is exercised in a small 6502 interpreter below rather than
by pattern-matching the bytes, so what is asserted is where the routine
writes, not what it looks like.  The interpreter covers only the opcodes
the status fragments emit and raises on anything else.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness import uci_network as u
from c64_test_harness.memory_policy import HARNESS_SCRATCH
from c64_test_harness.uci_network import (
    BIT_STAT_AV,
    UCI_CONTROL_STATUS_REG,
    UCI_STATUS_DATA_REG,
    _RESP_LEN_ADDR,
    _STAT_LEN_ADDR,
    _STATUS_ADDR,
    _build_read_status,
    _build_read_status_tsx,
    _read_status_string,
)

#: Room between the status buffer and the next field.  Derived from the
#: module's own layout, not restated, so moving either address moves it.
BUFFER = _RESP_LEN_ADDR - _STATUS_ADDR

#: The firmware's own ceiling on a status reply (command_intf.h:58).
FIRMWARE_MAX_STATUS = 256

FRAGMENT_ADDR = 0xC000


class _Queue6502:
    """Just enough 6502 to run a status-drain fragment against a fake UCI."""

    def __init__(self, code: list[int], status: bytes) -> None:
        self.mem = bytearray(0x10000)
        self.mem[FRAGMENT_ADDR:FRAGMENT_ADDR + len(code)] = bytes(code)
        self.end = FRAGMENT_ADDR + len(code)
        self.queue = list(status)
        self.consumed = 0
        self.writes: list[int] = []
        # A is whatever the previous fragment left: model it as non-zero so
        # a preamble that stores A instead of an explicit zero is visible.
        self.a = 0xA5
        self.x = self.y = 0
        self.z = self.c = False
        self.sp = 0xFF

    # -- bus ---------------------------------------------------------------
    def read(self, addr: int) -> int:
        if addr == UCI_CONTROL_STATUS_REG:
            return BIT_STAT_AV if self.queue else 0
        if addr == UCI_STATUS_DATA_REG:
            if not self.queue:
                return 0
            self.consumed += 1
            return self.queue.pop(0)
        return self.mem[addr]

    def write(self, addr: int, val: int) -> None:
        self.writes.append(addr)
        self.mem[addr] = val & 0xFF

    # -- cpu ---------------------------------------------------------------
    def _nz(self, v: int) -> int:
        v &= 0xFF
        self.z = v == 0
        return v

    def run(self, max_steps: int = 2_000_000) -> None:
        pc = FRAGMENT_ADDR
        m = self.mem
        for _ in range(max_steps):
            if pc == self.end:
                return
            op = m[pc]
            abs_ = m[(pc + 1) & 0xFFFF] | (m[(pc + 2) & 0xFFFF] << 8)
            imm = m[(pc + 1) & 0xFFFF]
            if op == 0xA9:   self.a = self._nz(imm); pc += 2            # LDA #
            elif op == 0xA2: self.x = self._nz(imm); pc += 2            # LDX #
            elif op == 0xA0: self.y = self._nz(imm); pc += 2            # LDY #
            elif op == 0xAD: self.a = self._nz(self.read(abs_)); pc += 3  # LDA abs
            elif op == 0x8D: self.write(abs_, self.a); pc += 3          # STA abs
            elif op == 0x8C: self.write(abs_, self.y); pc += 3          # STY abs
            elif op == 0x99: self.write((abs_ + self.y) & 0xFFFF, self.a); pc += 3
            elif op == 0x29: self.a = self._nz(self.a & imm); pc += 2   # AND #
            elif op == 0xC0:                                           # CPY #
                self.z = self.y == imm; self.c = self.y >= imm; pc += 2
            elif op == 0xC8: self.y = self._nz(self.y + 1); pc += 1     # INY
            elif op == 0x88: self.y = self._nz(self.y - 1); pc += 1     # DEY
            elif op == 0xCA: self.x = self._nz(self.x - 1); pc += 1     # DEX
            elif op == 0xAA: self.x = self._nz(self.a); pc += 1         # TAX
            elif op == 0x8A: self.a = self._nz(self.x); pc += 1         # TXA
            elif op == 0x48:                                           # PHA
                m[0x100 + self.sp] = self.a; self.sp = (self.sp - 1) & 0xFF; pc += 1
            elif op == 0x68:                                           # PLA
                self.sp = (self.sp + 1) & 0xFF
                self.a = self._nz(m[0x100 + self.sp]); pc += 1
            elif op in (0xF0, 0xD0):                                   # BEQ/BNE
                take = self.z if op == 0xF0 else not self.z
                off = imm - 0x100 if imm & 0x80 else imm
                pc = pc + 2 + (off if take else 0)
            elif op == 0x4C: pc = abs_                                  # JMP abs
            else:
                raise AssertionError(f"opcode ${op:02X} at ${pc:04X} not modelled")
        raise AssertionError("fragment did not terminate")


def _drain(kind: str, n: int) -> _Queue6502:
    status = bytes((0x30 + i % 10) for i in range(n))
    if kind == "plain":
        code = _build_read_status(_STATUS_ADDR, _STAT_LEN_ADDR)
    elif kind == "tsx-unfenced":
        code = _build_read_status_tsx(
            FRAGMENT_ADDR, _STATUS_ADDR, _STAT_LEN_ADDR, fence=False)
    else:
        raise ValueError(kind)
    cpu = _Queue6502(list(code), status)
    cpu.run()
    return cpu


KINDS = ["plain", "tsx-unfenced"]
#: Either side of the buffer edge, plus the firmware maximum.
LENGTHS = [0, 1, 28, BUFFER - 1, BUFFER, BUFFER + 1, 255, FIRMWARE_MAX_STATUS]


class TestTheLayoutPremise:
    def test_buffer_is_240_bytes(self) -> None:
        assert BUFFER == 240

    def test_firmware_may_send_more_than_the_buffer_holds(self) -> None:
        """Positive control on the premise: the clamp guards a real case."""
        assert FIRMWARE_MAX_STATUS > BUFFER

    def test_stat_len_field_is_outside_the_buffer(self) -> None:
        assert _STAT_LEN_ADDR >= _STATUS_ADDR + BUFFER


class TestTheInterpreterCanSeeAnOverrun:
    """Vacuity guard: the harness below must be able to fail.

    A fragment that stores every byte with no bound must be seen writing
    past the buffer; if this passed silently the drain tests would too.
    """

    def test_an_unbounded_store_is_caught(self) -> None:
        unbounded = [
            0xA0, 0x00,                                   # LDY #0
            0xAD, 0x1C, 0xDF, 0x29, BIT_STAT_AV,          # LDA $DF1C / AND
            0xF0, 0x09,                                   # BEQ done
            0xAD, 0x1F, 0xDF,                             # LDA $DF1F
            0x99, 0x00, 0xC3,                             # STA $C300,Y
            0xC8, 0xD0, 0xF0,                             # INY / BNE loop
        ]
        cpu = _Queue6502(unbounded, bytes(250))
        cpu.run()
        assert max(cpu.writes) >= _RESP_LEN_ADDR


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("n", LENGTHS)
class TestTheDrain:
    def test_never_stores_past_the_buffer(self, kind: str, n: int) -> None:
        cpu = _drain(kind, n)
        stray = [a for a in cpu.writes
                 if not (_STATUS_ADDR <= a < _STATUS_ADDR + BUFFER)
                 and a not in (_STAT_LEN_ADDR, _STAT_LEN_ADDR + 1)]
        assert not stray, (
            f"{n}-byte status wrote outside the buffer and length field: "
            + ", ".join(f"${a:04X}" for a in sorted(set(stray)))
        )

    def test_records_the_clamped_length_little_endian(
        self, kind: str, n: int
    ) -> None:
        cpu = _drain(kind, n)
        recorded = cpu.mem[_STAT_LEN_ADDR] | (cpu.mem[_STAT_LEN_ADDR + 1] << 8)
        assert recorded == min(n, BUFFER)

    def test_stores_the_bytes_that_fit(self, kind: str, n: int) -> None:
        cpu = _drain(kind, n)
        want = bytes((0x30 + i % 10) for i in range(min(n, BUFFER)))
        assert bytes(cpu.mem[_STATUS_ADDR:_STATUS_ADDR + len(want)]) == want

    def test_drains_the_whole_queue(self, kind: str, n: int) -> None:
        """Truncation must not leave status bytes behind the accept."""
        cpu = _drain(kind, n)
        assert cpu.consumed == n and not cpu.queue


class TestTheFencedDrainHasTheSameBound:
    """The fenced turbo variant runs in this interpreter, but its fence
    zeroes Y on every pass (issue #298), so behavioural results for it
    would be about that defect, not this one.  Pin structurally that it
    carries the same compare; the unfenced runs above check the loop."""

    def test_fenced_fragment_compares_y_against_the_buffer(self) -> None:
        code = list(_build_read_status_tsx(FRAGMENT_ADDR, _STATUS_ADDR,
                                           _STAT_LEN_ADDR, fence=True))
        cpy = [i for i in range(len(code) - 1)
               if code[i] == 0xC0 and code[i + 1] == BUFFER]
        assert cpy, "fenced status drain has no CPY #buffer bound"


def _transport(len_lo: int, len_hi: int, fill: int = 0x41) -> MagicMock:
    t = MagicMock()

    def read_memory(addr: int, length: int) -> bytes:
        if addr == _STAT_LEN_ADDR:
            return bytes([len_lo, len_hi])[:length]
        if addr == _STAT_LEN_ADDR + 1:
            return bytes([len_hi])
        return bytes([fill]) * length

    t.read_memory.side_effect = read_memory
    return t


def _status_reads(t: MagicMock) -> list[tuple[int, int]]:
    return [c.args for c in t.read_memory.call_args_list
            if c.args[0] not in (_STAT_LEN_ADDR, _STAT_LEN_ADDR + 1)]


class TestTheHostRead:
    def test_reads_both_length_bytes(self) -> None:
        t = _transport(0x05, 0x00)
        _read_status_string(t)
        covered = set()
        for addr, length in (c.args for c in t.read_memory.call_args_list):
            covered.update(range(addr, addr + length))
        assert _STAT_LEN_ADDR + 1 in covered, "high length byte never read"

    @pytest.mark.parametrize("lo,hi", [(0x2C, 0x01), (0xF1, 0x00), (0x00, 0x01),
                                       (0xFF, 0xFF)])
    def test_a_long_length_is_clamped_to_the_buffer(self, lo: int, hi: int) -> None:
        t = _transport(lo, hi)
        s = _read_status_string(t)
        for addr, length in _status_reads(t):
            assert addr + length <= _STATUS_ADDR + BUFFER, (
                f"read ${addr:04X}+{length} runs past the buffer"
            )
        assert len(s) == BUFFER

    def test_high_byte_alone_is_not_an_empty_status(self) -> None:
        """256 = $0100: a one-byte read sees 0 and reports no status."""
        assert _read_status_string(_transport(0x00, 0x01)) != ""

    def test_a_short_length_reads_exactly_that(self) -> None:
        t = _transport(17, 0)
        assert len(_read_status_string(t)) == 17

    def test_zero_length_is_empty(self) -> None:
        assert _read_status_string(_transport(0, 0)) == ""


class TestTheScratchSpan:
    def test_status_buffer_and_length_sit_inside_the_uci_stub_block(self) -> None:
        block = [r for r in HARNESS_SCRATCH
                 if r.purpose.startswith("UCI stub block")]
        assert len(block) == 1
        r = block[0]
        assert r.start <= _STATUS_ADDR and _STATUS_ADDR + BUFFER <= r.end
        assert r.start <= _STAT_LEN_ADDR and _STAT_LEN_ADDR + 2 <= r.end
