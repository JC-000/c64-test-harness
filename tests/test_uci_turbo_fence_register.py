"""Turbo UCI routines must keep their loop index across the fence (issue #298).

``_build_fence`` saves and restores A and X, but its delay loop counts with
``LDY #UCI_FENCE_INNER / DEY / BNE``, so it always exits with **Y = 0**.
Any turbo loop that indexes with Y and crosses a fence loses its index on
every pass.  Three loops did:

* ``_build_read_response_tsx`` -- every reply byte landed at the base
  address and the recorded length was 0;
* ``_build_read_status_tsx`` -- the same for the status string;
* the hostname loop in ``build_tcp_connect``/``build_udp_connect``
  (``turbo_safe=True``) -- ``INY`` after a fence always made Y = 1, so the
  loop re-sent ``host[1]`` forever and never reached the terminator.

These tests run **whole routines** in a small 6502 interpreter against a
fake UCI, and compare the turbo build with the plain build, which has no
fence and is the positive control: if the interpreter or the fake UCI were
wrong, the plain assertions would fail too.

Evidence grade: interpreter model only.  Nothing here has run on a device.
"""
from __future__ import annotations

import pytest

from c64_test_harness import uci_network as u
from c64_test_harness.uci_network import (
    BIT_CMD_BUSY,
    BIT_DATA_AV,
    BIT_ERROR,
    BIT_STAT_AV,
    CMD_ABORT,
    CMD_NEXT_DATA,
    CMD_PUSH,
    NET_CMD_GET_IPADDR,
    NET_CMD_SOCKET_READ,
    NET_CMD_TCP_CONNECT,
    NET_CMD_UDP_CONNECT,
    TARGET_NETWORK,
    UCI_CMD_DATA_REG,
    UCI_CONTROL_STATUS_REG,
    UCI_RESP_DATA_REG,
    UCI_STATUS_DATA_REG,
    _CODE_ADDR,
    _RESP_ADDR,
    _RESP_LEN_ADDR,
    _SENTINEL_ADDR,
    _SENTINEL_DONE,
    _STAT_LEN_ADDR,
    _STATUS_ADDR,
    build_get_ip,
    build_socket_read,
    build_tcp_connect,
    build_udp_connect,
)

#: Inputs the host stages, placed clear of every routine's footprint.  The
#: helpers' own ``$C100`` placement is overlapped by turbo code, which is a
#: separate defect; these tests isolate the index-register one.
HOST_ADDR = 0xC500
SOCKET_ID_ADDR = 0xC403

#: What unwritten reply memory holds, so a byte that never landed is visible.
UNWRITTEN = 0xEE

_STATE_BITS = 0x30


class _UciMachine:
    """Just enough 6502 + UCI to run a whole dispatched routine to its RTS."""

    def __init__(self, code: bytes, *, data: bytes = b"",
                 status: bytes = b"") -> None:
        self.mem = bytearray(0x10000)
        self.mem[_RESP_ADDR:_RESP_ADDR + 0x100] = bytes([UNWRITTEN]) * 0x100
        self.mem[_STATUS_ADDR:_STATUS_ADDR + 0xF0] = bytes([UNWRITTEN]) * 0xF0
        self.mem[_CODE_ADDR:_CODE_ADDR + len(code)] = code
        self._reply_data = list(data)
        self._reply_status = list(status)
        self.data_q: list[int] = []
        self.status_q: list[int] = []
        self.busy_state = False
        self.cmd_bytes: list[int] = []
        self.pushes = 0
        self.accepts = 0
        self.a = self.x = self.y = 0
        self.z = self.c = False
        self.sp = 0xFF

    # -- fake UCI ---------------------------------------------------------
    def read(self, addr: int) -> int:
        if addr == UCI_CONTROL_STATUS_REG:
            v = 0
            if self.data_q:
                v |= BIT_DATA_AV
            if self.status_q:
                v |= BIT_STAT_AV
            if self.busy_state:
                v |= 0x20          # STATE != idle until the accept
            return v & ~(BIT_CMD_BUSY | BIT_ERROR) & 0xFF
        if addr == UCI_RESP_DATA_REG:
            return self.data_q.pop(0) if self.data_q else 0
        if addr == UCI_STATUS_DATA_REG:
            return self.status_q.pop(0) if self.status_q else 0
        return self.mem[addr]

    def write(self, addr: int, val: int) -> None:
        val &= 0xFF
        if addr == UCI_CONTROL_STATUS_REG:
            if val == CMD_PUSH:
                self.pushes += 1
                self.data_q = list(self._reply_data)
                self.status_q = list(self._reply_status)
                self.busy_state = True
            elif val == CMD_NEXT_DATA:
                self.accepts += 1
                self.data_q = []
                self.status_q = []
                self.busy_state = False
            elif val == CMD_ABORT:
                pass
            return
        if addr == UCI_CMD_DATA_REG:
            self.cmd_bytes.append(val)
            return
        self.mem[addr] = val

    # -- cpu --------------------------------------------------------------
    def _nz(self, v: int) -> int:
        v &= 0xFF
        self.z = v == 0
        return v

    def _cmp(self, reg: int, imm: int) -> None:
        self.z = reg == imm
        self.c = reg >= imm

    def run(self, max_steps: int = 3_000_000) -> None:
        pc = _CODE_ADDR
        m = self.mem
        for _ in range(max_steps):
            op = m[pc]
            imm = m[(pc + 1) & 0xFFFF]
            abs_ = imm | (m[(pc + 2) & 0xFFFF] << 8)
            if op == 0x60:                                            # RTS
                if self.sp == 0xFF:
                    return
                raise AssertionError("RTS with a non-empty stack")
            elif op == 0xA9: self.a = self._nz(imm); pc += 2          # LDA #
            elif op == 0xA2: self.x = self._nz(imm); pc += 2          # LDX #
            elif op == 0xA0: self.y = self._nz(imm); pc += 2          # LDY #
            elif op == 0xAD: self.a = self._nz(self.read(abs_)); pc += 3
            elif op == 0xBD: self.a = self._nz(self.read((abs_ + self.x) & 0xFFFF)); pc += 3
            elif op == 0xB9: self.a = self._nz(self.read((abs_ + self.y) & 0xFFFF)); pc += 3
            elif op == 0xAC: self.y = self._nz(self.read(abs_)); pc += 3
            elif op == 0x8D: self.write(abs_, self.a); pc += 3        # STA abs
            elif op == 0x9D: self.write((abs_ + self.x) & 0xFFFF, self.a); pc += 3
            elif op == 0x99: self.write((abs_ + self.y) & 0xFFFF, self.a); pc += 3
            elif op == 0x8E: self.write(abs_, self.x); pc += 3        # STX abs
            elif op == 0x8C: self.write(abs_, self.y); pc += 3        # STY abs
            elif op == 0x29: self.a = self._nz(self.a & imm); pc += 2  # AND #
            elif op == 0x0D: self.a = self._nz(self.a | self.read(abs_)); pc += 3
            elif op == 0xE0: self._cmp(self.x, imm); pc += 2          # CPX #
            elif op == 0xC0: self._cmp(self.y, imm); pc += 2          # CPY #
            elif op == 0xE8: self.x = self._nz(self.x + 1); pc += 1   # INX
            elif op == 0xC8: self.y = self._nz(self.y + 1); pc += 1   # INY
            elif op == 0xCA: self.x = self._nz(self.x - 1); pc += 1   # DEX
            elif op == 0x88: self.y = self._nz(self.y - 1); pc += 1   # DEY
            elif op == 0xEE:                                          # INC abs
                self.write(abs_, self._nz(self.read(abs_) + 1)); pc += 3
            elif op == 0xCE:                                          # DEC abs
                self.write(abs_, self._nz(self.read(abs_) - 1)); pc += 3
            elif op == 0xAA: self.x = self._nz(self.a); pc += 1       # TAX
            elif op == 0x8A: self.a = self._nz(self.x); pc += 1       # TXA
            elif op == 0x48:                                          # PHA
                m[0x100 + self.sp] = self.a
                self.sp = (self.sp - 1) & 0xFF; pc += 1
            elif op == 0x68:                                          # PLA
                self.sp = (self.sp + 1) & 0xFF
                self.a = self._nz(m[0x100 + self.sp]); pc += 1
            elif op in (0xF0, 0xD0):                                  # BEQ/BNE
                take = self.z if op == 0xF0 else not self.z
                off = imm - 0x100 if imm & 0x80 else imm
                pc = pc + 2 + (off if take else 0)
            elif op == 0x4C: pc = abs_                                # JMP abs
            else:
                raise AssertionError(f"opcode ${op:02X} at ${pc:04X} not modelled")
        raise AssertionError("routine did not reach its RTS")

    # -- results ----------------------------------------------------------
    def resp_len(self) -> int:
        return self.mem[_RESP_LEN_ADDR]

    def stat_len(self) -> int:
        return self.mem[_STAT_LEN_ADDR] | (self.mem[_STAT_LEN_ADDR + 1] << 8)


def _payload(n: int, base: int = 0x30) -> bytes:
    return bytes((base + i) % 0x100 or 1 for i in range(n))


REPLY_LENGTHS = [0, 1, 2, 5, 17]
PATHS = [False, True]


def _run(code: bytes, **kw) -> _UciMachine:
    cpu = _UciMachine(code, **kw)
    cpu.run()
    assert cpu.mem[_SENTINEL_ADDR] == _SENTINEL_DONE
    return cpu


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
@pytest.mark.parametrize("n", REPLY_LENGTHS)
class TestTheResponseDrain:
    """``GET_IPADDR`` has no framing: the drain stores exactly the reply."""

    def test_reply_bytes_land_at_their_offsets(self, turbo: bool, n: int) -> None:
        reply = _payload(n)
        cpu = _run(build_get_ip(turbo_safe=turbo), data=reply)
        assert bytes(cpu.mem[_RESP_ADDR:_RESP_ADDR + n]) == reply
        assert cpu.mem[_RESP_ADDR + n] == UNWRITTEN, "stored past the reply"

    def test_recorded_length_is_the_reply_length(self, turbo: bool, n: int) -> None:
        cpu = _run(build_get_ip(turbo_safe=turbo), data=_payload(n))
        assert cpu.resp_len() == n

    def test_the_command_went_out_and_was_accepted_once(
        self, turbo: bool, n: int
    ) -> None:
        cpu = _run(build_get_ip(turbo_safe=turbo), data=_payload(n))
        assert cpu.cmd_bytes == [TARGET_NETWORK, NET_CMD_GET_IPADDR, 0x00]
        assert (cpu.pushes, cpu.accepts) == (1, 1)


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
@pytest.mark.parametrize("n", REPLY_LENGTHS)
class TestTheStatusDrain:
    def test_status_bytes_land_at_their_offsets(self, turbo: bool, n: int) -> None:
        status = _payload(n, base=0x41)
        cpu = _run(build_get_ip(turbo_safe=turbo), data=b"\x01", status=status)
        assert bytes(cpu.mem[_STATUS_ADDR:_STATUS_ADDR + n]) == status
        assert cpu.mem[_STATUS_ADDR + n] == UNWRITTEN

    def test_recorded_status_length(self, turbo: bool, n: int) -> None:
        cpu = _run(build_get_ip(turbo_safe=turbo), data=b"\x01",
                   status=_payload(n, base=0x41))
        assert cpu.stat_len() == n


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
class TestSocketReadDrainsTheFramedReply:
    def test_header_and_payload_land_and_length_counts_both(
        self, turbo: bool
    ) -> None:
        payload = _payload(9)
        block = bytes([len(payload), 0]) + payload
        code = build_socket_read(SOCKET_ID_ADDR, max_len=9, turbo_safe=turbo)
        cpu = _UciMachine(code, data=block)
        cpu.mem[SOCKET_ID_ADDR] = 0x07
        cpu.run()
        assert cpu.cmd_bytes == [TARGET_NETWORK, NET_CMD_SOCKET_READ, 0x07, 9, 0]
        assert bytes(cpu.mem[_RESP_ADDR:_RESP_ADDR + len(block)]) == block
        assert cpu.resp_len() == len(block)


HOSTS = [b"a", b"ab", b"10.0.0.2", b"host.example.org"]


@pytest.mark.parametrize("turbo", PATHS, ids=["plain", "turbo"])
@pytest.mark.parametrize("host", HOSTS, ids=lambda h: h.decode())
@pytest.mark.parametrize("builder,cmd", [
    (build_tcp_connect, NET_CMD_TCP_CONNECT),
    (build_udp_connect, NET_CMD_UDP_CONNECT),
], ids=["tcp", "udp"])
class TestTheHostnameLoop:
    def test_sends_the_hostname_then_one_terminator(
        self, turbo: bool, host: bytes, builder, cmd: int
    ) -> None:
        code = builder(HOST_ADDR, 0x1234, turbo_safe=turbo)
        cpu = _UciMachine(code, data=b"\x03")
        cpu.mem[HOST_ADDR:HOST_ADDR + len(host) + 1] = host + b"\x00"
        cpu.run()
        assert cpu.cmd_bytes == [TARGET_NETWORK, cmd, 0x34, 0x12, *host, 0x00]


class TestTheFenceStillClobbersY:
    """Premise: the defect is a Y-clobbering fence.  If the fence ever
    preserves Y this fails, and the register choice above can be revisited."""

    def test_fence_exits_with_y_zero(self) -> None:
        code = bytes([0xA0, 0x37] + u._build_fence() + [0x60])  # LDY #$37 ...
        cpu = _UciMachine(code)
        cpu.run()
        assert cpu.y == 0

    def test_fence_preserves_a_and_x(self) -> None:
        code = bytes([0xA9, 0x5A, 0xA2, 0xC3] + u._build_fence() + [0x60])
        cpu = _UciMachine(code)
        cpu.run()
        assert (cpu.a, cpu.x) == (0x5A, 0xC3)
